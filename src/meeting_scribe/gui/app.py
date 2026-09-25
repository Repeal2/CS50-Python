"""Tkinter control panel: record meetings, browse and search past ones, and change settings.

Windows desktop only (ships with Python's standard install there). Recording and the finish-up work
(transcription, pushing to Copilot Studio, saving) run on background threads so the UI doesn't freeze,
and so starting the next meeting doesn't have to wait for the previous one to finish (see session.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from meeting_scribe import __version__
from meeting_scribe.audio.device_watch import device_signature
from meeting_scribe.config import WHISPER_MODEL_SIZES, Settings, default_data_dir, load_settings, update_settings
from meeting_scribe.gui.meeting_prompt import PromptCard, start_recording_prompt, stop_recording_prompt
from meeting_scribe.gui.theme import Palette, apply_theme, style_text
from meeting_scribe.hotkeys import (
    GlobalHotkeyListener,
    HotkeyCombo,
    current_modifiers,
    is_modifier_keysym,
    key_name,
)
from meeting_scribe.screen.capture import ocr_region
from meeting_scribe.screen.meeting_detector import (
    DetectedMeeting,
    MeetingEndTracker,
    MeetingPromptTracker,
    detect_teams_meeting,
)
from meeting_scribe.screen.ocr_box import OcrAreaBox, default_area, keep_on_screen, overlaps
from meeting_scribe.screen.region_picker import RegionTarget, pick_region_interactively
from meeting_scribe.session import (
    CLOUD_ENGINE,
    LOCAL_ENGINE,
    MeetingSession,
    add_transcription,
    delete_meeting,
    meeting_in_progress,
    meeting_transcripts,
    retry_meeting_transcription,
)
from meeting_scribe.storage.database import Database, Meeting
from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text, save_original_copy
from meeting_scribe.transcription.engine import render_transcript

APP_NAME = "Meeting Scribe"

# Shown in a device dropdown in place of a device name, meaning "follow whatever Windows currently
# considers the default" rather than a specific device.
SYSTEM_DEFAULT_LABEL = "System default"
AUTO_HEADSET_LABEL = "Recognize by name"

# Used when recording starts with the project field blank, so a meeting never fails to start for lack of
# a project name. The project stays editable for the meeting's whole life, so this is never a dead end.
DEFAULT_PROJECT_NAME = "Unfiled"

# The title field's starting value — also what marks it as "not customized yet" for Start's Teams-name
# autofill (see RecordPage._detect_meeting_title).
DEFAULT_MEETING_TITLE = "Untitled meeting"

# Manual-notes editor: matches leading whitespace plus an optional bullet marker ("-", "*", or "•")
# followed by a space, so Enter can continue the same bullet/indent on the next line.
_NOTES_BULLET_PREFIX = re.compile(r"^[ \t]*(?:[-*•]\s+)?")
_NOTES_INDENT = "    "

_DOCUMENT_FILETYPES = [
    ("Supported documents", "*.pdf *.docx *.txt *.md *.png *.jpg *.jpeg *.bmp *.tiff"),
    ("All files", "*.*"),
]

_UNFINISHED_MEETING_NOTICE = (
    "Transcription didn't finish for this meeting, but the recording is safe. Retry to transcribe it."
)
_IN_PROGRESS_MEETING_NOTICE = "Still being recorded or transcribed — the transcript appears here when it's done."
_TRANSCRIBING_AGAIN_NOTICE = "Being transcribed again — the new transcript appears here when it's done."
_NOT_TRANSCRIBED_NOTICE = "No transcript {where}. Use “{button}” above to make one from the recording."
_SEARCH_PLACEHOLDER = "Search all meetings…"


# --- pure helpers ---------------------------------------------------------------------------------


def _meeting_transcripts(rows) -> tuple[str, str, str]:
    """(on-screen text, this PC's transcript, the cloud's transcript) rendered for the Library, from a
    meeting's saved transcript rows — see session.meeting_transcripts. Either transcript is "" if the
    meeting wasn't transcribed that way."""
    transcripts = meeting_transcripts(rows)
    return render_transcript(transcripts.screen), render_transcript(transcripts.local), render_transcript(transcripts.cloud)


def _format_meeting_timestamp(iso_string: str) -> str:
    """A stored UTC ISO timestamp in local time, human-readable."""
    try:
        return datetime.fromisoformat(iso_string).astimezone().strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return iso_string


def _format_short_date(iso_string: str) -> str:
    """"Sep 21" this year, "Sep 21, 2025" otherwise — for the Library's meeting list."""
    try:
        moment = datetime.fromisoformat(iso_string).astimezone()
    except ValueError:
        return iso_string
    return moment.strftime("%b %d") if moment.year == datetime.now().year else moment.strftime("%b %d, %Y")


def _format_duration(started_iso: str, ended_iso: str | None) -> str:
    """"42 min" / "1 h 05 min" between two stored timestamps; "" if unfinished or unreadable."""
    if not ended_iso:
        return ""
    try:
        seconds = (datetime.fromisoformat(ended_iso) - datetime.fromisoformat(started_iso)).total_seconds()
    except ValueError:
        return ""
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _format_elapsed(seconds: float) -> str:
    """A running recording clock: "04:09", or "1:02:03" past the hour."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _notes_timestamp(seconds: float) -> str:
    """The stamp Ctrl+T puts in the notes — the same [mm:ss] clock the transcript uses, so a note can be
    matched up with what was being said at the time."""
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"[{minutes:02d}:{secs:02d}] "


def _meeting_export_text(
    meeting: Meeting, project_name: str, local: str, screen: str, cloud: str = ""
) -> str:
    """Everything recorded for one meeting as a single plain-text document (Library's Export…). A meeting
    transcribed both ways gets both transcripts, each headed with where it came from."""
    sections = [
        f"{meeting.title}\n"
        f"Project: {project_name}\n"
        f"Date: {_format_meeting_timestamp(meeting.started_at)}"
        + (f" ({_format_duration(meeting.started_at, meeting.ended_at)})" if meeting.ended_at else "")
    ]
    transcripts = (
        (("Transcript (this PC)", local), ("Transcript (cloud)", cloud))
        if local.strip() and cloud.strip()
        else (("Transcript", local or cloud),)
    )
    for heading, body in (
        *transcripts,
        ("On-screen text", screen),
        ("Notes", meeting.manual_notes),
        ("Attendees", meeting.attendees),
    ):
        if body and body.strip():
            sections.append(f"{heading}\n{'-' * len(heading)}\n{body.strip()}")
    return "\n\n".join(sections) + "\n"


def _safe_filename(name: str) -> str:
    return " ".join(re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).split()) or "meeting"


def _has_no_mic_signal(problems: "tuple[str, ...]") -> bool:
    """Whether `problems` (from MeetingSession.input_problems()) includes the mic reading digital silence
    — what flashes the OCR box red. Matches audio.recorder._describe_stream_problem's wording."""
    return any(problem.startswith("Microphone:") and "digital silence" in problem for problem in problems)


def _device_choices(
    device_names: "list[str]", selected: str, default_label: str = SYSTEM_DEFAULT_LABEL
) -> "list[str]":
    """Dropdown values for a device picker: the default label, every device currently present, and the
    selected device even if it's unplugged right now — resetting it would silently lose the choice."""
    values = [default_label] + [name for name in device_names if name != default_label]
    if selected and selected not in values:
        values.append(selected)
    return values


def _device_from_choice(choice: str, default_label: str = SYSTEM_DEFAULT_LABEL) -> str | None:
    return None if choice == default_label else choice


def _hotkey_label(combo: HotkeyCombo | None) -> str:
    return combo.label if combo is not None else "Not set"


def _enumerate_devices() -> tuple[list, list]:
    """A fresh (input devices, loopback devices) enumeration. Empty lists off Windows."""
    from meeting_scribe.audio.device_picker import list_input_devices, list_loopback_devices

    try:
        return list_input_devices(), list_loopback_devices()
    except RuntimeError:
        return [], []


def _open_path(path: Path) -> None:
    """Opens a folder or file with whatever the OS uses for it (Explorer on Windows)."""
    if sys.platform == "win32":
        # Under the Microsoft Store Python, writes to AppData are redirected into the package's private
        # LocalCache, which Explorer (outside the package) can't see; realpath maps to where it really is.
        os.startfile(os.path.realpath(path))  # noqa: S606 — a local folder chosen by the app itself
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _is_store_python_private(path: str) -> bool:
    return "\\packages\\pythonsoftwarefoundation.python." in path.lower()


def _enable_per_monitor_dpi_awareness() -> None:
    """Marks this process per-monitor DPI aware, so Windows reports physical-pixel window and monitor
    coordinates instead of virtualizing them per monitor — which would throw win32gui.GetWindowRect and
    mss's screen capture out of step with each other on a mixed-DPI setup. Must run before any window
    is created."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # coarser system-DPI-aware fallback (Vista+)
        except (AttributeError, OSError):
            pass


# --- small widgets --------------------------------------------------------------------------------


def _card(parent: tk.Misc, title: str | None = None, *, padding: int = 16) -> tuple[ttk.Frame, ttk.Frame]:
    """A bordered surface with an optional title. Returns (card, body) — pack/grid the card, fill the body."""
    card = ttk.Frame(parent, style="Card.TFrame")
    body = ttk.Frame(card, style="Card.TFrame", borderwidth=0, padding=padding)
    body.pack(fill="both", expand=True, padx=1, pady=1)
    if title:
        ttk.Label(body, text=title, style="Card.Title.TLabel").pack(anchor="w", pady=(0, 10))
    return card, body


def _scrolled_text(parent: tk.Misc, palette: Palette, font, **text_options) -> tuple[ttk.Frame, tk.Text]:
    frame = ttk.Frame(parent, style="Card.TFrame", borderwidth=0)
    text = tk.Text(frame, wrap="word", **text_options)
    style_text(text, palette, font)
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    return frame, text


def _wrap_to_width(label: ttk.Label, container: tk.Misc, *, margin: int) -> None:
    """Keeps a label's text wrapped to its container's current width, less `margin` pixels."""
    container.bind("<Configure>", lambda e: label.configure(wraplength=max(120, e.width - margin)), add="+")


def _open_dropdown_on_click(combo: ttk.Combobox) -> None:
    """An editable combobox (project/title) only opens its suggestion list when the narrow arrow is
    clicked — everywhere else in the field, a click just places the text cursor. Bind a click anywhere
    in the field to also post the dropdown, so it behaves like a normal dropdown to click into, while a
    second click (or typing) still edits the text underneath exactly as before. Bound at the instance
    level, which Tk dispatches before the combobox's own class-level click binding, so this only adds
    the extra "also post" behavior rather than replacing cursor placement or text selection."""

    def on_click(event: tk.Event) -> None:
        if "textarea" in combo.identify(event.x, event.y) and str(combo.cget("state")) != "disabled":
            combo.tk.call("ttk::combobox::Post", combo)

    combo.bind("<Button-1>", on_click, add="+")


def _set_readonly_text(widget: tk.Text, content: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    widget.configure(state="disabled")


class _PlaceholderEntry(ttk.Entry):
    """An entry showing muted hint text while it's empty and unfocused."""

    def __init__(self, parent: tk.Misc, placeholder: str, **options):
        self.var = tk.StringVar()
        super().__init__(parent, textvariable=self.var, **options)
        self._placeholder = placeholder
        self._showing = False
        self.bind("<FocusIn>", self._hide)
        self.bind("<FocusOut>", self._show)
        self._show()

    def value(self) -> str:
        return "" if self._showing else self.var.get()

    def _show(self, _event=None) -> None:
        if not self.var.get():
            self._showing = True
            self.configure(style="Placeholder.TEntry")
            self.var.set(self._placeholder)

    def _hide(self, _event=None) -> None:
        if self._showing:
            self._showing = False
            self.var.set("")
            self.configure(style="TEntry")


class _ScrollableFrame(ttk.Frame):
    """A vertically scrolling page — Settings grows taller than a small window."""

    def __init__(self, parent: tk.Misc, palette: Palette):
        super().__init__(parent)
        self._canvas = tk.Canvas(self, background=palette.background, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner = ttk.Frame(self._canvas, padding=(28, 24))
        window = self._canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self.inner.bind("<Configure>", lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(window, width=e.width))
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        # One app-wide handler that only acts over this page (<Enter>/<Leave> on the frame itself also
        # fire when the pointer crosses into its own children, so they can't gate this).
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_wheel(self, event) -> None:
        over_page = str(event.widget).startswith(str(self)) and self.winfo_ismapped()
        if over_page and self.inner.winfo_height() > self._canvas.winfo_height():
            self._canvas.yview_scroll(int(-event.delta / 120), "units")


# --- the app --------------------------------------------------------------------------------------


class MeetingScribeApp(tk.Tk):
    _DEVICE_POLL_MS = 2000  # how often device dropdowns check for devices coming and going
    _MEETING_POLL_MS = 2000  # how often to check whether a Teams call has started
    _CLOCK_MS = 500

    def __init__(self, settings: Settings | None = None):
        _enable_per_monitor_dpi_awareness()
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x760")
        self.minsize(940, 640)
        self.palette, self.fonts = apply_theme(self)

        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        self._session: MeetingSession | None = None
        self._session_started_at: float | None = None
        # Finish-up jobs (a stopped meeting's transcription, a Library retry) still running — see
        # run_in_background and _on_close.
        self._background_threads: list[threading.Thread] = []
        # A windowed PyInstaller build has no console, so an exception inside a Tk callback would
        # otherwise vanish; log it next to the database instead.
        self.report_callback_exception = self._report_callback_exception

        self._build_layout()
        if self.settings.moved_from is not None:
            self.record_page.log(f"Moved your meeting data from {self.settings.moved_from} to {self.settings.data_dir}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<Control-f>", lambda _e: self.show_page("library", focus_search=True))

        self._hotkey_listener: GlobalHotkeyListener | None = None
        self.apply_hotkeys()

        # State for _poll_devices: the last device signature seen while idle, and the last recorder
        # device-list version seen while recording (paired with the session it came from).
        self._idle_device_signature = device_signature()
        self._seen_device_list: tuple[MeetingSession | None, int] = (None, 0)
        self.after(self._DEVICE_POLL_MS, self._poll_devices)
        self.after(self._CLOCK_MS, self._tick_clock)

        self._meeting_prompt_tracker = MeetingPromptTracker()
        self._meeting_end_tracker = MeetingEndTracker()
        self._start_prompt: PromptCard | None = None
        self._stop_prompt: PromptCard | None = None
        self._detected_meeting: DetectedMeeting | None = None
        # The work area of the monitor the call was last seen on — where the "meeting ended" prompt goes.
        self._meeting_work_area: dict | None = None
        if sys.platform == "win32":
            self.after(self._MEETING_POLL_MS, self._poll_teams_meeting)

    # -- layout / navigation ----------------------------------------------------------------------

    def _build_layout(self) -> None:
        sidebar = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 18), width=224)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="◉ " + APP_NAME, style="Brand.TLabel").pack(anchor="w", padx=6, pady=(0, 22))

        self._content = ttk.Frame(self)
        self._content.pack(side="left", fill="both", expand=True)

        self.record_page = RecordPage(self._content, self)
        self.library_page = LibraryPage(self._content, self)
        self.settings_page = SettingsPage(self._content, self)
        self._pages = {"record": self.record_page, "library": self.library_page, "settings": self.settings_page}
        self._nav_buttons: dict[str, ttk.Button] = {}
        for key, label in (("record", "●   Record"), ("library", "▤   Library"), ("settings", "⚙   Settings")):
            button = ttk.Button(sidebar, text=label, style="Nav.TButton", command=lambda k=key: self.show_page(k))
            button.pack(fill="x", pady=2)
            self._nav_buttons[key] = button

        ttk.Label(sidebar, text=f"Version {__version__}", style="Sidebar.Muted.TLabel").pack(
            side="bottom", anchor="w", padx=6
        )
        self._sidebar_status = ttk.Label(sidebar, text="", style="Sidebar.Muted.TLabel", wraplength=180)
        self._sidebar_status.pack(side="bottom", anchor="w", padx=6, pady=(0, 8))

        self._toast = ttk.Label(self._content, style="Pill.TLabel")
        self._toast_after_id: str | None = None
        self.show_page("record")

    def show_page(self, key: str, *, focus_search: bool = False) -> None:
        for name, page in self._pages.items():
            if name == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
            self._nav_buttons[name].configure(style="NavActive.TButton" if name == key else "Nav.TButton")
        if key == "library":
            self.library_page.on_show(focus_search=focus_search)

    def toast(self, message: str) -> None:
        """A short, non-blocking confirmation at the bottom of the window — for things that went fine and
        need no answer, where a modal "OK" dialog would just be one more click."""
        if self._toast_after_id is not None:
            self.after_cancel(self._toast_after_id)
        self._toast.configure(text=message)
        self._toast.place(relx=0.5, rely=1.0, y=-18, anchor="s")
        self._toast.lift()
        self._toast_after_id = self.after(3200, self._toast.place_forget)

    def _tick_clock(self) -> None:
        """Keeps the recording clock, window title and sidebar status current."""
        try:
            finishing = sum(thread.is_alive() for thread in self._background_threads)
            if self._session is not None and self._session_started_at is not None:
                elapsed = _format_elapsed(time.monotonic() - self._session_started_at)
                self.title(f"● {elapsed} — {APP_NAME}")
                status = f"Recording · {elapsed}"
            else:
                elapsed = None
                self.title(APP_NAME)
                status = "Ready"
            if finishing:
                status += f"\nFinishing {finishing} meeting{'s' if finishing > 1 else ''}…"
            self._sidebar_status.configure(text=status)
            self.record_page.show_elapsed(elapsed)
        finally:
            self.after(self._CLOCK_MS, self._tick_clock)

    # -- recording lifecycle ------------------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        if self._session_started_at is None:
            return 0.0
        return time.monotonic() - self._session_started_at

    def set_session(self, session: MeetingSession | None) -> None:
        self._session = session
        self._session_started_at = time.monotonic() if session is not None else None
        self.close_meeting_prompts()

    def apply_hotkeys(self) -> None:
        """(Re)starts the global-hotkey listener from the current start/stop combos."""
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            self._hotkey_listener = None

        bindings = {}
        if self.settings.start_meeting_hotkey is not None:
            bindings[1] = (self.settings.start_meeting_hotkey, lambda: self.after(0, self._handle_start_hotkey))
        if self.settings.stop_meeting_hotkey is not None:
            bindings[2] = (self.settings.stop_meeting_hotkey, lambda: self.after(0, self.record_page.stop))
        if not bindings:
            return

        listener = GlobalHotkeyListener(bindings)
        try:
            failed = listener.start()
        except RuntimeError:
            return  # not on Windows — hotkeys just aren't available there
        self._hotkey_listener = listener
        if failed:
            labels = ", ".join(combo.label for combo in failed)
            messagebox.showwarning(
                APP_NAME, f"Couldn't register this shortcut — it's likely used by another application: {labels}"
            )

    def _handle_start_hotkey(self) -> None:
        # A global hotkey bypasses button state, so it needs its own guard against a second meeting.
        if self._session is None:
            self.record_page.start()

    def run_in_background(self, target: Callable[[], None]) -> None:
        """Runs a meeting's finish-up work on a daemon thread, remembered so closing the window while it's
        still running can warn first — see _on_close."""
        self._background_threads = [thread for thread in self._background_threads if thread.is_alive()]
        thread = threading.Thread(target=target, daemon=True)
        self._background_threads.append(thread)
        thread.start()

    # -- devices ------------------------------------------------------------------------------------

    def refresh_device_lists(self) -> None:
        """Repopulates the device dropdowns. While a meeting records, the list has to come from its
        recorder — a fresh enumeration would only see PortAudio's snapshot (see audio.device_watch)."""
        session = self._session
        inputs, loopbacks = session.available_devices() if session is not None else _enumerate_devices()
        self.record_page.apply_device_lists(inputs, loopbacks)
        self.settings_page.apply_device_lists(inputs)

    def request_device_refresh(self) -> None:
        """The refresh button. Idle, that's a fresh enumeration. While recording, the recorder restarts
        its audio to see new devices, which blocks briefly — so that runs in the background, and
        _poll_devices picks up the new list."""
        session = self._session
        if session is None:
            self.refresh_device_lists()
            self.toast("Device list refreshed")
            return

        def worker() -> None:
            try:
                session.reload_devices()
            except Exception as exc:
                self.after(0, self.record_page.log, f"[{session.title}] Couldn't refresh devices: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _poll_devices(self) -> None:
        """Keeps the device dropdowns current as devices come and go, without anyone pressing refresh."""
        try:
            session = self._session
            if session is not None:
                version = session.device_list_version
                if self._seen_device_list != (session, version):
                    first_look = self._seen_device_list[0] is not session
                    self._seen_device_list = (session, version)
                    if not first_look:
                        self.refresh_device_lists()
            else:
                signature = device_signature()
                if signature is not None and signature != self._idle_device_signature:
                    if self._idle_device_signature is not None:
                        self.refresh_device_lists()
                    self._idle_device_signature = signature
        finally:
            self.after(self._DEVICE_POLL_MS, self._poll_devices)

    def switch_active_recording(
        self, *, mic_changed: bool, mic_name: str | None, system_changed: bool, system_name: str | None
    ) -> None:
        """Moves a meeting that's recording right now onto newly chosen device(s). Runs in the
        background — it can block briefly joining the old capture thread."""
        session = self._session
        if session is None or not (mic_changed or system_changed):
            return

        def worker() -> None:
            if mic_changed:
                self._switch_one_device(session.switch_mic_device, mic_name, "Microphone")
            if system_changed:
                self._switch_one_device(session.switch_system_device, system_name, "System audio")

        threading.Thread(target=worker, daemon=True).start()

    def _switch_one_device(self, switch: Callable[[str | None], None], device_name: str | None, label: str) -> None:
        try:
            switch(device_name)
        except Exception as exc:
            self.after(0, self._on_device_switch_failed, label, exc)
        else:
            self.after(0, self.record_page.log, f"{label} switched to {device_name or SYSTEM_DEFAULT_LABEL!r}.")

    def _on_device_switch_failed(self, label: str, exc: Exception) -> None:
        self.record_page.log(f"{label} switch failed: {exc}")
        messagebox.showerror(APP_NAME, f"Couldn't switch {label.lower()}: {exc}")

    # -- Teams call detection ---------------------------------------------------------------------

    def _poll_teams_meeting(self) -> None:
        """Checks for a Teams call on a background thread (it enumerates every window — too slow for the
        GUI thread every couple of seconds). The next check is scheduled only once this one reports."""

        def worker() -> None:
            try:
                meeting = detect_teams_meeting()
            except Exception:  # a win32/registry call failing is never worth more than a missed check
                meeting = None
            try:
                self.after(0, self._on_teams_meeting_polled, meeting)
            except (RuntimeError, tk.TclError):
                pass  # the app closed while this was running

        threading.Thread(target=worker, daemon=True).start()

    def _on_teams_meeting_polled(self, meeting: DetectedMeeting | None) -> None:
        try:
            self._detected_meeting = meeting
            in_call = meeting is not None
            session = self._session
            recording = session is not None
            window_rect = self._measure_meeting_window(meeting) if meeting is not None else None

            if self._meeting_prompt_tracker.update(in_call=in_call, recording=recording) and meeting is not None:
                if self._start_prompt is None:
                    self._start_prompt = start_recording_prompt(
                        self, meeting.name, self._start_from_meeting_prompt, self._dismiss_start_prompt
                    )
                if self._meeting_work_area is not None:
                    self._start_prompt.place(window_rect, self._meeting_work_area)
            else:
                self._close_start_prompt()

            if self._meeting_end_tracker.update(in_call=in_call, recording=recording) and session is not None:
                work_area = self._meeting_work_area or self._primary_work_area()
                if self._stop_prompt is None and work_area is not None:
                    self._stop_prompt = stop_recording_prompt(
                        self, session.title, self._stop_from_meeting_prompt, self._dismiss_stop_prompt
                    )
                    self._stop_prompt.place(None, work_area)
            else:
                self._close_stop_prompt()
        finally:
            self.after(self._MEETING_POLL_MS, self._poll_teams_meeting)

    def _measure_meeting_window(self, meeting: DetectedMeeting) -> dict | None:
        from meeting_scribe.screen.window_picker import get_monitor_work_area, get_window_region

        hwnd = meeting.window.hwnd if meeting.window is not None else None
        try:
            window_rect = get_window_region(hwnd) if hwnd is not None else None
            self._meeting_work_area = get_monitor_work_area(hwnd)
        except Exception:
            return None  # the call window closed since the check ran
        return window_rect

    def _primary_work_area(self) -> dict | None:
        from meeting_scribe.screen.window_picker import get_monitor_work_area

        try:
            return get_monitor_work_area(None)
        except Exception:
            return None

    def close_meeting_prompts(self) -> None:
        self._close_start_prompt()
        self._close_stop_prompt()

    def _close_start_prompt(self) -> None:
        if self._start_prompt is not None:
            self._start_prompt.close()
            self._start_prompt = None

    def _close_stop_prompt(self) -> None:
        if self._stop_prompt is not None:
            self._stop_prompt.close()
            self._stop_prompt = None

    def _dismiss_start_prompt(self) -> None:
        self._meeting_prompt_tracker.dismiss()
        self._close_start_prompt()

    def _dismiss_stop_prompt(self) -> None:
        self._meeting_end_tracker.dismiss()
        self._close_stop_prompt()

    def _start_from_meeting_prompt(self) -> None:
        meeting = self._detected_meeting
        self._dismiss_start_prompt()
        if self._session is not None:
            return
        if meeting is not None and meeting.name:
            if self.record_page.title_var.get().strip() in ("", DEFAULT_MEETING_TITLE):
                self.record_page.title_var.set(meeting.name)
        self.record_page.start()

    def _stop_from_meeting_prompt(self) -> None:
        self._dismiss_stop_prompt()
        self.record_page.stop()

    # -- projects / documents -----------------------------------------------------------------------

    def refresh_project_lists(self) -> None:
        self.record_page.refresh_projects()
        self.library_page.refresh()

    def prompt_new_project(self) -> str | None:
        name = simpledialog.askstring("New project", "Project name:", parent=self)
        if not name or not name.strip():
            return None
        project = self.db.get_or_create_project(name.strip())
        self.refresh_project_lists()
        return project.name

    def attach_document(self, project_name: str, meeting_id: int | None) -> str | None:
        """Asks for a file, extracts its text, keeps a copy of the original (the Copilot push hands the
        real file off under its own name) and files it under the project, and the meeting if one is
        given. Returns the file's name, or None if nothing was attached."""
        path_str = filedialog.askopenfilename(title="Attach a document", filetypes=_DOCUMENT_FILETYPES)
        if not path_str:
            return None
        path = Path(path_str)
        try:
            text = extract_text(path, tesseract_cmd=self.settings.tesseract_cmd)
        except UnsupportedDocumentError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return None
        except Exception as exc:  # a corrupt PDF/DOCX, an unreadable image, Tesseract missing
            messagebox.showerror(APP_NAME, f"Couldn't read {path.name!r}: {exc}")
            return None
        source_path = save_original_copy(path, self.settings.documents_dir)
        project = self.db.get_or_create_project(project_name)
        self.db.add_document(project.id, path.name, text, meeting_id=meeting_id, source_path=str(source_path))
        return path.name

    # -- shutdown -----------------------------------------------------------------------------------

    def _on_close(self) -> None:
        """Asks first if a meeting is recording or finishing, and stops a recording cleanly — its audio
        kept, so it can be transcribed later with Retry in the Library."""
        session = self._session
        finishing = any(thread.is_alive() for thread in self._background_threads)
        if session is not None or finishing:
            if session is not None:
                message = (
                    f'"{session.title}" is still recording. Close anyway?\n\nRecording will stop and the '
                    "audio will be kept — you can transcribe it later with Retry in the Library."
                )
            else:
                message = (
                    "A meeting is still being transcribed in the background. Close anyway?\n\nIt will be "
                    "left unfinished — you can transcribe it later with Retry in the Library."
                )
            if not messagebox.askyesno(APP_NAME, message, icon="warning", parent=self):
                return
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        if session is not None:
            self.record_page.flush_manual_notes()
            self._session = None
            try:
                session.abandon()
            except Exception:  # closing must still go ahead; the audio on disk is what matters
                self._report_callback_exception(*sys.exc_info())
        self.record_page.close_ocr_box()
        self.db.close()
        self.destroy()

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        try:
            with open(self.settings.data_dir / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
        except OSError:
            pass


# --- Record ---------------------------------------------------------------------------------------


class RecordPage(ttk.Frame):
    # Long enough to say a sentence into the microphone, short enough that nobody skips the test.
    MIC_TEST_SECONDS = 3.0
    _IDLE_HINT = "An OCR box appears on screen when recording starts — drag it over the captions, then press Start OCR."
    _RECORDING_HINT = "Recording. Stopping transcribes in the background, so you can start the next meeting right away."

    def __init__(self, parent: tk.Misc, app: MeetingScribeApp):
        super().__init__(parent, padding=(28, 24))
        self.app = app
        palette, fonts = app.palette, app.fonts
        # The on-screen OCR box for the meeting being recorded — see screen.ocr_box. None while idle.
        self._ocr_box: OcrAreaBox | None = None
        # Which session's recorder notices have already been logged, how many, and which live input
        # problems have been mentioned — see _refresh_input_warnings.
        self._notice_session = None
        self._notices_logged = 0
        self._logged_input_problems: set[str] = set()
        self._manual_notes_save_after_id: str | None = None
        # A mic test's result holds the line under the meters for a few seconds — see _on_mic_test_done.
        self._mic_test_result: str | None = None

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="Record", style="Heading.TLabel").pack(side="left")
        self._rec_pill = ttk.Label(header, style="RecPill.TLabel")

        # Meeting card: what's being recorded, and the one big button.
        meeting_card, meeting = _card(self)
        meeting_card.pack(fill="x")
        meeting.columnconfigure(1, weight=1)
        meeting.columnconfigure(4, weight=1)

        ttk.Label(meeting, text="Project", style="Card.Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(meeting, textvariable=self.project_var)
        self.project_combo.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 0))
        for sequence in ("<<ComboboxSelected>>", "<FocusOut>", "<Return>"):
            self.project_combo.bind(sequence, self._on_project_field_committed)
        _open_dropdown_on_click(self.project_combo)
        ttk.Button(meeting, text="New…", style="Link.TButton", command=self._new_project).grid(
            row=1, column=2, padx=(6, 18), pady=(2, 0)
        )

        ttk.Label(meeting, text="Meeting title", style="Card.Muted.TLabel").grid(row=0, column=3, sticky="w")
        self.title_var = tk.StringVar(value=DEFAULT_MEETING_TITLE)
        # Editable — the dropdown just suggests titles already used in this project, for repeat meetings.
        self.title_combo = ttk.Combobox(meeting, textvariable=self.title_var)
        self.title_combo.grid(row=1, column=3, columnspan=2, sticky="we", pady=(2, 0))
        self.title_var.trace_add("write", self._on_title_changed)
        _open_dropdown_on_click(self.title_combo)

        actions = ttk.Frame(meeting, style="Card.TFrame", borderwidth=0)
        actions.grid(row=2, column=0, columnspan=5, sticky="we", pady=(16, 0))
        self.record_button = ttk.Button(actions, text="●  Start recording", style="Record.TButton", command=self.start)
        self.record_button.pack(side="left")
        ttk.Button(actions, text="Reset OCR box", style="Link.TButton", command=self._reset_ocr_box).pack(side="right")
        ttk.Button(actions, text="Attach document", style="Link.TButton", command=self._upload_document).pack(
            side="right"
        )
        self.capture_attendees_button = ttk.Button(
            actions, text="Capture attendees", style="Link.TButton", command=self._capture_attendees,
            state="disabled",
        )
        self.capture_attendees_button.pack(side="right")
        self._hint_var = tk.StringVar(value=self._IDLE_HINT)
        ttk.Label(meeting, textvariable=self._hint_var, style="Card.Muted.TLabel").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(8, 0)
        )

        # Audio card: device pickers with live meters right next to them.
        audio_card, audio = _card(self, padding=14)
        audio_card.pack(fill="x", pady=(14, 0))
        audio.columnconfigure(2, weight=1)
        self.mic_var = tk.StringVar(value=app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.system_var = tk.StringVar(value=app.settings.system_device_name or SYSTEM_DEFAULT_LABEL)
        self.mic_combo, self.mic_level_bar = self._device_row(audio, 0, "Microphone", self.mic_var)
        self.system_combo, self.system_level_bar = self._device_row(audio, 1, "System audio", self.system_var)
        device_buttons = ttk.Frame(audio, style="Card.TFrame", borderwidth=0)
        device_buttons.grid(row=0, column=3, rowspan=2, sticky="e", padx=(12, 0))
        self.test_mic_button = ttk.Button(device_buttons, text="Test mic", command=self._test_microphone)
        self.test_mic_button.pack(fill="x")
        ttk.Button(device_buttons, text="↻ Refresh", command=app.request_device_refresh).pack(fill="x", pady=(4, 0))
        # A meter can't tell "muted" from "nobody is talking" — this says which the app thinks it is.
        # Inline rather than a dialog: a modal mid-meeting would steal focus from the call.
        self._input_warning = ttk.Label(audio, style="Card.Danger.TLabel", wraplength=760, justify="left")
        self._input_warning.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._audio_status = ""
        self._set_audio_status("")

        # Notes beside the activity log.
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(14, 0))

        notes_card, notes = _card(panes, padding=0)
        notes_header = ttk.Frame(notes, style="Card.TFrame", borderwidth=0, padding=(14, 10, 10, 4))
        notes_header.pack(fill="x")
        ttk.Label(notes_header, text="Notes", style="Card.Title.TLabel").pack(side="left")
        ttk.Button(notes_header, text="Insert time  Ctrl+T", style="Link.TButton", command=self._insert_timestamp).pack(
            side="right"
        )
        notes_frame, self.manual_notes_editor = _scrolled_text(notes, palette, fonts.body, undo=True)
        notes_frame.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        self.manual_notes_editor.insert(
            "1.0",
            "Notes open here when recording starts, and save as you type.\n\n"
            "- Start a line with \"- \" and Enter continues the bullet\n"
            "    - Tab and Shift+Tab indent and outdent\n"
            "- Ctrl+T stamps the recording time, to match the transcript",
        )
        self.manual_notes_editor.configure(state="disabled", foreground=palette.muted)
        self.manual_notes_editor.bind("<KeyRelease>", self._schedule_manual_notes_save)
        self.manual_notes_editor.bind("<Return>", self._on_manual_notes_return)
        self.manual_notes_editor.bind("<Tab>", self._on_manual_notes_indent)
        self.manual_notes_editor.bind("<Shift-Tab>", self._on_manual_notes_dedent)
        self.manual_notes_editor.bind("<Control-t>", self._insert_timestamp)
        panes.add(notes_card, weight=3)

        log_card, log = _card(panes, padding=0)
        log_header = ttk.Frame(log, style="Card.TFrame", borderwidth=0, padding=(14, 10, 10, 4))
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Activity", style="Card.Title.TLabel").pack(side="left")
        ttk.Button(log_header, text="Clear", style="Link.TButton", command=self._clear_log).pack(side="right")
        log_frame, self.output = _scrolled_text(log, palette, fonts.mono, state="disabled")
        self.output.configure(foreground=palette.muted)
        log_frame.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        panes.add(log_card, weight=2)

        self.refresh_projects()
        self.apply_device_lists(*_enumerate_devices())
        self._poll_audio_levels()

    def _device_row(self, parent, row: int, label: str, var: tk.StringVar):
        ttk.Label(parent, text=label, style="Card.TLabel", width=12).grid(row=row, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(parent, textvariable=var, state="readonly", width=38)
        combo.grid(row=row, column=1, sticky="w", padx=(0, 14), pady=4)
        combo.bind("<<ComboboxSelected>>", self._on_devices_changed)
        meter = ttk.Progressbar(parent, style="Meter.Horizontal.TProgressbar", maximum=100, mode="determinate")
        meter.grid(row=row, column=2, sticky="we", pady=4)
        return combo, meter

    # -- header / clock -----------------------------------------------------------------------------

    def show_elapsed(self, elapsed: str | None) -> None:
        if elapsed is None:
            self._rec_pill.pack_forget()
        else:
            self._rec_pill.configure(text=f"●  Recording  {elapsed}")
            self._rec_pill.pack(side="right")

    # -- projects / titles --------------------------------------------------------------------------

    def refresh_projects(self) -> None:
        self.project_combo["values"] = [p.name for p in self.app.db.list_projects()]

    def _new_project(self) -> None:
        name = self.app.prompt_new_project()
        if name:
            self.project_var.set(name)
            self._on_project_field_committed()

    def _refresh_title_suggestions(self) -> None:
        project = self.app.db.get_project_by_name(self.project_var.get().strip())
        self.title_combo["values"] = self.app.db.list_recent_meeting_titles(project.id) if project else []

    def _on_project_field_committed(self, _event=None) -> None:
        """Runs when the project field is committed (focus leaves, Enter, or a pick from the list) rather
        than on every keystroke — moving a meeting creates the project, which mustn't happen per letter.
        Refreshes the title suggestions and, mid-meeting, moves the meeting to this project."""
        self._refresh_title_suggestions()
        session = self.app._session
        project_name = self.project_var.get().strip()
        if session is None or not project_name or project_name == session.project.name:
            return
        session.set_project(project_name)
        self.log(f'[{session.title}] Moved to project "{project_name}".')

    def _on_title_changed(self, *_args) -> None:
        """The title stays editable for the whole meeting; each change is saved straight away."""
        session = self.app._session
        if session is None:
            return
        session.title = self.title_var.get()
        self.app.db.update_meeting_title(session.meeting_id, session.title)

    # -- audio ----------------------------------------------------------------------------------------

    def apply_device_lists(self, input_devices, loopback_devices) -> None:
        from meeting_scribe.audio.device_picker import full_device_name

        input_names = [d.name for d in input_devices]
        # A microphone saved by an older version under MME's cut-off name: show and keep its full one.
        upgraded = full_device_name(self.mic_var.get(), input_names)
        if upgraded != self.mic_var.get():
            self.mic_var.set(upgraded)
            if self.app.settings.mic_device_name is not None:
                self.app.settings = update_settings(self.app.settings, mic_device_name=upgraded)
        self.mic_combo["values"] = _device_choices(input_names, self.mic_var.get())
        self.system_combo["values"] = _device_choices([d.name for d in loopback_devices], self.system_var.get())

    def _on_devices_changed(self, _event=None) -> None:
        """Saves the device choice immediately and, mid-meeting, moves the recording onto it."""
        mic_name = _device_from_choice(self.mic_var.get())
        system_name = _device_from_choice(self.system_var.get())
        mic_changed = mic_name != self.app.settings.mic_device_name
        system_changed = system_name != self.app.settings.system_device_name
        self.app.settings = update_settings(
            self.app.settings, mic_device_name=mic_name, system_device_name=system_name
        )
        self.app.switch_active_recording(
            mic_changed=mic_changed, mic_name=mic_name, system_changed=system_changed, system_name=system_name
        )

    def _poll_audio_levels(self) -> None:
        session = self.app._session
        mic_level, system_level = session.audio_levels() if session is not None else (0.0, 0.0)
        self.mic_level_bar["value"] = mic_level * 100
        self.system_level_bar["value"] = system_level * 100
        self._refresh_input_warnings(session)
        self.after(150, self._poll_audio_levels)

    def _refresh_input_warnings(self, session) -> None:
        """Surfaces anything wrong with what's being captured while it can still be fixed. Recorder
        notices (a dead capture thread, a substituted device) are facts and go to the activity log once
        each; input problems (silence, clipping) are a live judgement, so they hold the warning line for
        as long as they last. A dead-silent mic also flashes the OCR box red, where the user is looking."""
        if session is not self._notice_session:
            self._notice_session = session
            self._notices_logged = 0
            self._logged_input_problems = set()
        if session is None:
            if self._mic_test_result is None:
                self._set_audio_status("")
            return

        notices = session.capture_notices()
        for message in notices[self._notices_logged:]:
            self.log(f"[{session.title}] {message}")
        self._notices_logged = len(notices)

        problems = session.input_problems()
        if problems or self._mic_test_result is None:
            self._set_audio_status("\n".join(f"⚠  {problem}" for problem in problems))
        for problem in problems:
            if problem not in self._logged_input_problems:
                self._logged_input_problems.add(problem)
                self.log(f"[{session.title}] {problem}")
        if self._ocr_box is not None:
            self._ocr_box.set_alert(_has_no_mic_signal(problems))

    def _set_audio_status(self, text: str, style: str = "Card.Danger.TLabel") -> None:
        """The line under the meters: live input problems, or a mic test's result. Hidden when empty."""
        if text == self._audio_status and str(self._input_warning.cget("style")) == style:
            return
        self._audio_status = text
        self._input_warning.configure(text=text, style=style)
        if text:
            self._input_warning.grid()
        else:
            self._input_warning.grid_remove()

    def _test_microphone(self) -> None:
        """Listens to the selected microphone for a few seconds and reports what it heard. Works mid-meeting
        too — WASAPI shared mode lets the device be opened twice."""
        from meeting_scribe.audio.recorder import check_input_device

        device_name = _device_from_choice(self.mic_var.get())
        self.test_mic_button.configure(state="disabled", text="Listening…")
        self._mic_test_result = "running"
        self._set_audio_status(
            f"Listening for {self.MIC_TEST_SECONDS:.0f} seconds — say something.", "Card.Muted.TLabel"
        )

        def worker() -> None:
            try:
                result = check_input_device(device_name, seconds=self.MIC_TEST_SECONDS)
            except Exception as exc:
                self.after(0, self._on_mic_test_done, f"Couldn't test that microphone: {exc}", False)
                return
            self.after(0, self._on_mic_test_done, result.summary, result.is_ok)

        threading.Thread(target=worker, daemon=True).start()

    def _on_mic_test_done(self, summary: str, ok: bool) -> None:
        """Shown under the meters (not in a dialog) for a few seconds, and logged."""
        self.test_mic_button.configure(state="normal", text="Test mic")
        self.log(f"Mic test: {summary}")
        self._set_audio_status(("✓  " if ok else "⚠  ") + summary, "Card.Success.TLabel" if ok else "Card.Danger.TLabel")
        self._mic_test_result = summary
        self.after(8000, self._clear_mic_test_result, summary)

    def _clear_mic_test_result(self, summary: str) -> None:
        if self._mic_test_result == summary:
            self._mic_test_result = None
            self._set_audio_status("")

    # -- the OCR box --------------------------------------------------------------------------------

    def _virtual_screen(self) -> dict | None:
        """Every monitor together, as one mss-style rect — what the box is kept inside."""
        try:
            import mss

            with mss.mss() as sct:
                return dict(sct.monitors[0])
        except Exception:
            return None

    def _default_ocr_area(self) -> dict:
        """Over the bottom of the Teams call window if one is open (where live captions appear),
        otherwise the middle of the main screen."""
        from meeting_scribe.screen.window_picker import (
            find_teams_meeting_window,
            get_monitor_work_area,
            get_window_region,
        )

        window_rect = None
        try:
            teams = find_teams_meeting_window()
            if teams is not None:
                window_rect = get_window_region(teams.hwnd)
        except Exception:
            window_rect = None
        try:
            work_area = get_monitor_work_area(None)
        except Exception:
            work_area = {"left": 0, "top": 0, "width": self.winfo_screenwidth(), "height": self.winfo_screenheight()}
        return default_area(work_area, window_rect)

    def _initial_ocr_area(self) -> dict:
        """Where the last meeting's box was left, if that's still on a connected screen; else the default."""
        saved = self.app.settings.ocr_area
        screen = self._virtual_screen()
        if saved is not None:
            area = dict(zip(("left", "top", "width", "height"), saved))
            if screen is None:
                return area
            if overlaps(area, screen):
                return keep_on_screen(area, screen)
        area = self._default_ocr_area()
        return keep_on_screen(area, screen) if screen is not None else area

    def _show_ocr_box(self, session: MeetingSession, area: dict) -> None:
        self.close_ocr_box()

        def area_changed(new_area: dict) -> None:
            if self.app._session is session:
                session.set_screen_area(new_area)
            self._remember_ocr_area(new_area)

        def toggle() -> None:
            if self.app._session is not session or self._ocr_box is None:
                return
            reading = not session.screen_reading
            session.set_screen_reading(reading)
            self._ocr_box.set_reading(reading)
            area_now = self._ocr_box.area
            if reading:
                self.log(f"[{session.title}] OCR started — reading the {area_now['width']}x{area_now['height']} box.")
            else:
                self.log(f"[{session.title}] OCR paused.")

        try:
            self._ocr_box = OcrAreaBox(
                self, area, on_area_changed=area_changed, on_toggle=toggle, bounds=self._virtual_screen()
            )
        except Exception as exc:  # the meeting still records audio without it
            self._ocr_box = None
            self.log(f"[{session.title}] Couldn't show the OCR box: {exc}")

    def _remember_ocr_area(self, area: dict) -> None:
        self.app.settings = update_settings(
            self.app.settings, ocr_area=(area["left"], area["top"], area["width"], area["height"])
        )

    def _reset_ocr_box(self) -> None:
        """Puts the box back at its default place. Works mid-meeting too; the area being read moves with it."""
        area = self._default_ocr_area()
        screen = self._virtual_screen()
        if screen is not None:
            area = keep_on_screen(area, screen)
        self._remember_ocr_area(area)
        session = self.app._session
        if self._ocr_box is not None:
            self._ocr_box.set_area(area)
            if session is not None:
                session.set_screen_area(area)
        self.app.toast("OCR box moved back to its default place")

    def close_ocr_box(self) -> None:
        if self._ocr_box is not None:
            self._ocr_box.close()
            self._ocr_box = None

    # -- activity log ----------------------------------------------------------------------------------

    def log(self, message: str) -> None:
        """Appends a timestamped line to the activity log, and to activity.log beside the database — the
        widget only lives in memory, and a native crash would otherwise take its last lines with it."""
        line = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        self.output.configure(state="normal")
        self.output.insert("end", line + "\n")
        self.output.configure(state="disabled")
        self.output.see("end")
        try:
            with open(self.app.settings.data_dir / "activity.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
        except OSError:
            pass

    def _clear_log(self) -> None:
        _set_readonly_text(self.output, "")

    # -- start / stop ----------------------------------------------------------------------------------

    def _detect_meeting_title(self) -> str | None:
        """Best-effort Teams meeting name for a still-default title. Never stops a meeting starting."""
        from meeting_scribe.screen.window_picker import find_teams_meeting_name

        try:
            return find_teams_meeting_name()
        except Exception:
            return None

    def start(self) -> None:
        if self.app._session is not None:
            return
        project_name = self.project_var.get().strip() or DEFAULT_PROJECT_NAME
        meeting_title = self.title_var.get().strip() or DEFAULT_MEETING_TITLE
        if meeting_title == DEFAULT_MEETING_TITLE:
            meeting_title = self._detect_meeting_title() or DEFAULT_MEETING_TITLE
        ocr_area = self._initial_ocr_area()
        try:
            session = MeetingSession(
                self.app.settings,
                self.app.db,
                project_name,
                meeting_title,
                screen_target=RegionTarget(**ocr_area),
                read_screen=False,
            )
            session.start()
        except Exception as exc:  # a missing/unavailable audio device raises OSError, not RuntimeError
            # Shown, not just logged: a failed Start from the global hotkey otherwise looks like nothing.
            messagebox.showerror(APP_NAME, f"Couldn't start recording: {exc}")
            return

        self.app.set_session(session)
        # Reflect the fallbacks back into the fields, so what's shown is what's being recorded.
        self.project_var.set(project_name)
        self.title_var.set(meeting_title)
        self.record_button.configure(text="■  Stop recording", style="Stop.TButton", command=self.stop)
        self.capture_attendees_button.configure(state="normal")
        self._hint_var.set(self._RECORDING_HINT)

        self.manual_notes_editor.configure(state="normal", foreground=self.app.palette.text)
        self.manual_notes_editor.delete("1.0", "end")
        self.manual_notes_editor.edit_reset()
        self.manual_notes_editor.focus_set()

        # The log isn't cleared: a previous meeting may still be logging its finish-up, and the [title]
        # prefix keeps overlapping meetings' lines apart.
        self.log(f"[{meeting_title}] Recording started in project {project_name!r}.")
        self._show_ocr_box(session, ocr_area)

    def stop(self) -> None:
        session = self.app._session
        if session is None:
            return
        # Save the last keystrokes before the session is detached — _save_manual_notes reads it.
        self.flush_manual_notes()
        # Detach right away: transcribing can take a while, and shouldn't block starting the next meeting.
        self.app.set_session(None)

        self.record_button.configure(text="●  Start recording", style="Record.TButton", command=self.start)
        self.capture_attendees_button.configure(state="disabled")
        self._hint_var.set(self._IDLE_HINT)
        self.manual_notes_editor.configure(state="disabled")
        self.log(f"[{session.title}] Stopped — transcribing in the background.")
        self.close_ocr_box()
        settings = self.app.settings

        def worker() -> None:
            def report(message: str) -> None:
                self.after(0, self.log, f"[{session.title}] {message}")

            try:
                # The Settings in force now, not when the meeting started: unticking the cloud mid-meeting
                # applies to this meeting.
                session.stop(on_progress=report, settings=settings)
            except Exception as exc:
                self.after(0, self._on_stop_failed, session.title, exc)
                return
            self.after(0, self._on_stop_done, session.title)

        self.app.run_in_background(worker)

    def _on_stop_done(self, meeting_title: str) -> None:
        self.log(f"[{meeting_title}] Done — it's in the Library.")
        self.app.toast(f"“{meeting_title}” is saved and transcribed")
        self.app.refresh_project_lists()

    def _on_stop_failed(self, meeting_title: str, exc: Exception) -> None:
        self.log(f"[{meeting_title}] Failed: {exc}")
        self.app.refresh_project_lists()
        messagebox.showerror(
            APP_NAME, f'Couldn\'t finish "{meeting_title}": {exc}\n\nThe recording is kept — use Retry in the Library.'
        )

    # -- notes ------------------------------------------------------------------------------------------

    def _schedule_manual_notes_save(self, _event=None) -> None:
        """Debounced: saves ~400ms after typing pauses rather than on every keystroke."""
        if self._manual_notes_save_after_id is not None:
            self.after_cancel(self._manual_notes_save_after_id)
        self._manual_notes_save_after_id = self.after(400, self._save_manual_notes)

    def flush_manual_notes(self) -> None:
        if self._manual_notes_save_after_id is not None:
            self.after_cancel(self._manual_notes_save_after_id)
            self._manual_notes_save_after_id = None
        self._save_manual_notes()

    def _save_manual_notes(self) -> None:
        self._manual_notes_save_after_id = None
        session = self.app._session
        if session is not None:
            self.app.db.set_manual_notes(session.meeting_id, self.manual_notes_editor.get("1.0", "end-1c"))

    def _on_manual_notes_return(self, event) -> str:
        """Continues the current line's indentation and bullet marker onto the new line."""
        widget = event.widget
        line_text = widget.get("insert linestart", "insert lineend")
        match = _NOTES_BULLET_PREFIX.match(line_text)
        widget.insert("insert", "\n" + (match.group(0) if match else ""))
        widget.see("insert")
        return "break"

    def _on_manual_notes_indent(self, event) -> str:
        event.widget.insert("insert linestart", _NOTES_INDENT)
        return "break"

    def _on_manual_notes_dedent(self, event) -> str:
        widget = event.widget
        line_start = widget.index("insert linestart")
        line_text = widget.get(line_start, "insert lineend")
        remove = min(len(line_text) - len(line_text.lstrip(" ")), len(_NOTES_INDENT))
        if remove:
            widget.delete(line_start, f"{line_start}+{remove}c")
        return "break"

    def _insert_timestamp(self, _event=None) -> str:
        """Stamps the note with the recording clock, matching the transcript's [mm:ss]."""
        if self.app._session is not None:
            self.manual_notes_editor.insert("insert", _notes_timestamp(self.app.elapsed_seconds()))
            self.manual_notes_editor.focus_set()
            self._schedule_manual_notes_save()
        return "break"

    # -- documents / attendees -------------------------------------------------------------------------

    def _upload_document(self) -> None:
        """Attaches a document to the project entered here — and to the meeting, if one is recording."""
        project_name = self.project_var.get().strip()
        if not project_name:
            messagebox.showinfo(APP_NAME, "Choose or enter a project first.")
            return
        session = self.app._session
        name = self.app.attach_document(project_name, session.meeting_id if session is not None else None)
        if name is None:
            return
        self.app.refresh_project_lists()
        if session is not None:
            self.log(f"[{session.title}] Document attached: {name}")
            self.app.toast(f"Attached {name} to this meeting")
        else:
            self.app.toast(f"Attached {name} to “{project_name}”")

    def _capture_attendees(self) -> None:
        """Drag a box over a participants panel; it's read immediately and added to the attendee list."""
        session = self.app._session
        if session is None:
            return
        region = pick_region_interactively(self)
        if region is None:
            return
        try:
            text = ocr_region(region.mss_region, tesseract_cmd=self.app.settings.tesseract_cmd)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Couldn't read that area: {exc}")
            return
        if not text.strip():
            self.app.toast("No text found in that area")
            return
        existing = self.app.db.get_meeting(session.meeting_id).attendees or ""
        self.app.db.set_attendees(session.meeting_id, (existing + "\n" + text).strip())
        count = len(text.strip().splitlines())
        self.log(f"[{session.title}] Attendees captured ({count} line(s)).")
        self.app.toast(f"Captured {count} attendee line(s)")


# --- Library --------------------------------------------------------------------------------------


class LibraryPage(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: MeetingScribeApp):
        super().__init__(parent, padding=(28, 24))
        self.app = app
        palette = app.palette
        self._projects: dict[str, object] = {}  # tree item id -> Project
        self._meetings: dict[str, Meeting] = {}  # tree item id -> Meeting
        self._documents: list = []
        self._current: Meeting | None = None
        self._search_after_id: str | None = None

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="Library", style="Heading.TLabel").pack(side="left")
        self.search = _PlaceholderEntry(header, _SEARCH_PLACEHOLDER, width=34)
        self.search.pack(side="right")
        self.search.var.trace_add("write", self._on_search_changed)
        self.search.bind("<Escape>", lambda _e: self._clear_search())

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # Left: projects over meetings.
        browse_card, browse = _card(panes, padding=0)
        panes.add(browse_card, weight=1)
        projects_header = ttk.Frame(browse, style="Card.TFrame", borderwidth=0, padding=(14, 10, 8, 2))
        projects_header.pack(fill="x")
        ttk.Label(projects_header, text="Projects", style="Card.Title.TLabel").pack(side="left")
        ttk.Button(projects_header, text="+ New", style="Link.TButton", command=self._new_project).pack(side="right")
        self.project_tree = ttk.Treeview(browse, show="tree", selectmode="browse", height=6)
        self.project_tree.column("#0", width=260)
        self.project_tree.pack(fill="x", padx=6)
        self.project_tree.bind("<<TreeviewSelect>>", self._on_project_selected)

        self._meetings_label = ttk.Label(browse, text="Meetings", style="Card.Title.TLabel", padding=(14, 12, 8, 2))
        self._meetings_label.pack(fill="x")
        meetings_frame = ttk.Frame(browse, style="Card.TFrame", borderwidth=0)
        meetings_frame.pack(fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        self.meeting_tree = ttk.Treeview(meetings_frame, columns=("when",), show="tree", selectmode="browse")
        self.meeting_tree.column("#0", width=180, stretch=True)
        self.meeting_tree.column("when", width=80, stretch=False, anchor="e")
        self.meeting_tree.tag_configure("unfinished", foreground=palette.danger)
        meetings_scroll = ttk.Scrollbar(meetings_frame, orient="vertical", command=self.meeting_tree.yview)
        self.meeting_tree.configure(yscrollcommand=meetings_scroll.set)
        meetings_scroll.pack(side="right", fill="y")
        self.meeting_tree.pack(side="left", fill="both", expand=True)
        self.meeting_tree.bind("<<TreeviewSelect>>", self._on_meeting_selected)
        self.meeting_tree.bind("<Delete>", lambda _e: self._delete_meeting())

        # Right: the selected meeting.
        detail_card, detail = _card(panes, padding=0)
        panes.add(detail_card, weight=3)
        top = self._detail_top = ttk.Frame(detail, style="Card.TFrame", borderwidth=0, padding=(18, 14, 12, 8))
        top.pack(fill="x")
        self._title_label = ttk.Label(top, text="No meeting selected", style="Card.Title.TLabel")
        self._title_label.pack(anchor="w")
        self._meta_label = ttk.Label(top, style="Card.Muted.TLabel", justify="left")
        self._meta_label.pack(anchor="w", pady=(2, 0))
        _wrap_to_width(self._meta_label, top, margin=30)
        toolbar = ttk.Frame(top, style="Card.TFrame", borderwidth=0)
        toolbar.pack(fill="x", pady=(8, 0))
        self._action_buttons = []
        for text, command in (
            ("Copy", self._copy_current_tab),
            ("Export…", self._export),
            ("Attach document", self._upload_document),
            ("Open folder", self._open_folder),
            ("Delete…", self._delete_meeting),
        ):
            button = ttk.Button(toolbar, text=text, style="Link.TButton", command=command, state="disabled")
            button.pack(side="left", padx=(0, 4))
            self._action_buttons.append(button)
        # Transcribe a finished meeting's recording again, one way or the other — e.g. to the cloud, for a
        # meeting only transcribed on this PC.
        self._transcribe_buttons: dict[str, ttk.Button] = {}
        for engine, text in ((CLOUD_ENGINE, "Transcribe in cloud"), (LOCAL_ENGINE, "Transcribe on this PC")):
            button = ttk.Button(
                toolbar, text=text, style="Link.TButton", state="disabled",
                command=lambda e=engine: self._transcribe_again(e),
            )
            button.pack(side="right", padx=(4, 0))
            self._transcribe_buttons[engine] = button

        # Shown only for a meeting whose recording finished but whose transcription didn't.
        self._banner = ttk.Frame(detail, style="Card.TFrame", borderwidth=0, padding=(18, 0, 12, 8))
        self._banner_label = ttk.Label(self._banner, style="Banner.TLabel", justify="left")
        self.retry_button = ttk.Button(self._banner, text="Retry", style="Accent.TButton",
                                       command=self._retry_transcription)
        self.retry_button.pack(side="right", padx=(10, 0))
        self._banner_label.pack(side="left", fill="x", expand=True)
        _wrap_to_width(self._banner_label, self._banner, margin=190)

        self.detail_notebook = ttk.Notebook(detail)
        self.detail_notebook.pack(fill="both", expand=True, padx=(8, 2), pady=(0, 2))
        self._toolbar = toolbar
        self._empty = ttk.Label(
            detail, text="Pick a meeting on the left to see its transcript, notes and documents.\n"
            "Search looks through every project's titles, transcripts, notes and attendees.",
            style="Card.Muted.TLabel", justify="center", anchor="center",
        )
        self.local_text = self._text_tab("Transcript · this PC")
        self.cloud_text = self._text_tab("Transcript · cloud")
        self.ocr_text = self._text_tab("On screen")
        self.manual_notes_text = self._text_tab("Notes")
        self.attendees_text = self._text_tab("Attendees")
        self._build_documents_tab()

        self.refresh()

    def _text_tab(self, title: str) -> tk.Text:
        frame, text = _scrolled_text(self.detail_notebook, self.app.palette, self.app.fonts.body, state="disabled")
        self.detail_notebook.add(frame, text=title)
        return text

    def _build_documents_tab(self) -> None:
        frame = ttk.Frame(self.detail_notebook, style="Card.TFrame", borderwidth=0)
        self.detail_notebook.add(frame, text="Documents")
        self.document_tree = ttk.Treeview(frame, show="tree", selectmode="browse", height=8)
        self.document_tree.column("#0", width=200)
        self.document_tree.pack(side="left", fill="y", pady=6)
        self.document_tree.bind("<<TreeviewSelect>>", self._on_document_selected)
        ttk.Separator(frame, orient="vertical").pack(side="left", fill="y")
        viewer_frame, self.document_viewer = _scrolled_text(
            frame, self.app.palette, self.app.fonts.body, state="disabled"
        )
        viewer_frame.pack(side="left", fill="both", expand=True)

    # -- lists --------------------------------------------------------------------------------------

    def on_show(self, *, focus_search: bool) -> None:
        if focus_search:
            self.search.focus_set()
            self.search.select_range(0, "end")

    def refresh(self) -> None:
        """Reloads the lists, keeping the current selection where it still exists."""
        selected_project = self._selected_project()
        current_id = self._current.id if self._current is not None else None
        self.project_tree.delete(*self.project_tree.get_children())
        self._projects = {}
        for project in self.app.db.list_projects():
            item = self.project_tree.insert("", "end", text="  " + project.name)
            self._projects[item] = project
            if selected_project is not None and project.id == selected_project.id and not self.search.value():
                self.project_tree.selection_set(item)
        self._reload_meetings(select_id=current_id)

    def _selected_project(self):
        selection = self.project_tree.selection()
        return self._projects.get(selection[0]) if selection else None

    def _reload_meetings(self, *, select_id: int | None = None) -> None:
        query = self.search.value().strip()
        project = self._selected_project()
        if query:
            meetings = self.app.db.search_meetings(query)
            project_names = {p.id: p.name for p in self._projects.values()}
            self._meetings_label.configure(text=f"{len(meetings)} result{'' if len(meetings) == 1 else 's'}")
        elif project is not None:
            meetings = self.app.db.list_meetings(project.id)
            project_names = {}
            self._meetings_label.configure(text=f"Meetings ({len(meetings)})")
        else:
            meetings, project_names = [], {}
            self._meetings_label.configure(text="Meetings")

        self.meeting_tree.delete(*self.meeting_tree.get_children())
        self._meetings = {}
        reselect = None
        for meeting in meetings:
            when = _format_short_date(meeting.started_at)
            if project_names:
                when = project_names.get(meeting.project_id, "")
            tags = ("unfinished",) if meeting.ended_at is None else ()
            item = self.meeting_tree.insert("", "end", text="  " + meeting.title, values=(when,), tags=tags)
            self._meetings[item] = meeting
            if meeting.id == select_id:
                reselect = item
        if reselect is None and query and self._meetings:
            reselect = next(iter(self._meetings))  # jump straight to the best (newest) match
        if reselect is not None:
            self.meeting_tree.selection_set(reselect)
            self.meeting_tree.see(reselect)
        else:
            self._show_meeting(None)

    def _on_project_selected(self, _event=None) -> None:
        if self._selected_project() is not None and self.search.value():
            self._clear_search()
        # Keeps the open meeting open if it's in this project — a refresh re-fires this event.
        self._reload_meetings(select_id=self._current.id if self._current is not None else None)

    def _on_search_changed(self, *_args) -> None:
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(250, self._run_search)

    def _run_search(self) -> None:
        self._search_after_id = None
        if self.search.value().strip():
            self.project_tree.selection_remove(*self.project_tree.selection())
        self._reload_meetings(select_id=self._current.id if self._current is not None else None)

    def _clear_search(self) -> None:
        if self.search.value():
            self.search.var.set("")
            if self.focus_get() is not self.search:
                self.search._show()

    def _new_project(self) -> None:
        name = self.app.prompt_new_project()
        for item, project in self._projects.items():
            if project.name == name:
                self._clear_search()
                self.project_tree.selection_set(item)
                self.project_tree.see(item)

    def _on_meeting_selected(self, _event=None) -> None:
        selection = self.meeting_tree.selection()
        self._show_meeting(self._meetings.get(selection[0]) if selection else None)

    # -- the selected meeting ---------------------------------------------------------------------------

    def _show_meeting(self, meeting: Meeting | None) -> None:
        if meeting is not None:
            meeting = self.app.db.get_meeting(meeting.id)  # always show the latest saved state
        self._current = meeting
        for button in self._action_buttons:
            button.configure(state="normal" if meeting is not None else "disabled")
        for widget in (self._empty, self._detail_top, self._banner, self.detail_notebook):
            widget.pack_forget()
        if meeting is None:
            self._empty.pack(fill="both", expand=True)
            return
        self._detail_top.pack(fill="x")

        project = self.app.db.get_project(meeting.project_id)
        meta = [project.name if project else "", _format_meeting_timestamp(meeting.started_at)]
        duration = _format_duration(meeting.started_at, meeting.ended_at)
        if duration:
            meta.append(duration)
        if meeting.meeting_code:
            meta.append(f"ID {meeting.meeting_code}")
        self._title_label.configure(text=meeting.title)
        self._meta_label.configure(text="   ·   ".join(part for part in meta if part))

        screen, local, cloud = _meeting_transcripts(self.app.db.get_segments(meeting.id))
        finished = meeting.ended_at is not None
        _set_readonly_text(self.local_text, local or (
            _NOT_TRANSCRIBED_NOTICE.format(where="on this PC", button="Transcribe on this PC") if finished else ""
        ))
        _set_readonly_text(self.cloud_text, cloud or (
            _NOT_TRANSCRIBED_NOTICE.format(where="in the cloud", button="Transcribe in cloud") if finished else ""
        ))
        if finished and not local and cloud and self.detail_notebook.index("current") == 0:
            self.detail_notebook.select(1)  # open on the transcript there is
        _set_readonly_text(self.ocr_text, screen or "No on-screen text was captured.")
        _set_readonly_text(self.manual_notes_text, meeting.manual_notes or "No notes were taken.")
        _set_readonly_text(self.attendees_text, meeting.attendees or "No attendees were captured.")
        self._load_documents(meeting.id)

        in_progress = meeting_in_progress(meeting.id)
        for button in self._transcribe_buttons.values():
            button.configure(state="normal" if finished and not in_progress else "disabled")
        if not finished:
            self._show_banner(
                _IN_PROGRESS_MEETING_NOTICE if in_progress else _UNFINISHED_MEETING_NOTICE, retry=not in_progress
            )
        elif in_progress:
            self._show_banner(_TRANSCRIBING_AGAIN_NOTICE, retry=False)
        self.detail_notebook.pack(fill="both", expand=True, padx=(8, 2), pady=(0, 2))

    def _load_documents(self, meeting_id: int | None) -> None:
        self._documents = self.app.db.list_documents_for_meeting(meeting_id) if meeting_id is not None else []
        self.document_tree.delete(*self.document_tree.get_children())
        for index, document in enumerate(self._documents):
            self.document_tree.insert("", "end", iid=str(index), text="  " + document["filename"])
        _set_readonly_text(
            self.document_viewer,
            "" if meeting_id is None else ("Select a document to read its text." if self._documents
                                           else "No documents are attached to this meeting."),
        )

    def _on_document_selected(self, _event=None) -> None:
        selection = self.document_tree.selection()
        if selection:
            _set_readonly_text(self.document_viewer, self._documents[int(selection[0])]["content_text"])

    def _copy_current_tab(self) -> None:
        tab = self.detail_notebook.index("current")
        widget = [
            self.local_text, self.cloud_text, self.ocr_text, self.manual_notes_text, self.attendees_text,
            self.document_viewer,
        ][tab]
        self.clipboard_clear()
        self.clipboard_append(widget.get("1.0", "end-1c"))
        self.app.toast(f"Copied {self.detail_notebook.tab(tab, 'text').lower()} to the clipboard")

    def _export(self) -> None:
        meeting = self._current
        if meeting is None:
            return
        project = self.app.db.get_project(meeting.project_id)
        screen, local, cloud = _meeting_transcripts(self.app.db.get_segments(meeting.id))
        default_name = _safe_filename(f"{meeting.meeting_code or ''} {meeting.title}".strip()) + ".txt"
        path = filedialog.asksaveasfilename(
            title="Export meeting", defaultextension=".txt", initialfile=default_name,
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        Path(path).write_text(
            _meeting_export_text(meeting, project.name if project else "", local, screen, cloud), encoding="utf-8"
        )
        self.app.toast(f"Exported to {Path(path).name}")

    def _open_folder(self) -> None:
        if self._current is None:
            return
        folder = self.app.settings.meeting_dir(self._current.id)
        try:
            _open_path(folder if folder.exists() else self.app.settings.data_dir)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't open {folder}: {exc}")

    def _upload_document(self) -> None:
        """Attaches a document after the fact, to the selected meeting."""
        meeting = self._current
        if meeting is None:
            return
        project = self.app.db.get_project(meeting.project_id)
        name = self.app.attach_document(project.name, meeting.id)
        if name is not None:
            self._load_documents(meeting.id)
            self.app.toast(f"Attached {name} to “{meeting.title}”")

    def _delete_meeting(self) -> None:
        """Deletes the selected meeting and everything recorded for it, after asking."""
        meeting = self._current
        if meeting is None:
            return
        if meeting_in_progress(meeting.id):
            messagebox.showinfo(
                APP_NAME, f"“{meeting.title}” is still being recorded or transcribed — it can be deleted once that's done."
            )
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"Delete “{meeting.title}” ({_format_meeting_timestamp(meeting.started_at)})?\n\n"
            "Its recording, transcripts, on-screen text, notes, attendees and attached documents are removed "
            "from this PC. Anything already sent to Copilot Studio stays there. This can't be undone.",
            icon="warning",
            default="no",
        ):
            return
        try:
            left_behind = delete_meeting(self.app.settings, self.app.db, meeting.id)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._current = None
        self.app.refresh_project_lists()
        if left_behind:
            messagebox.showwarning(
                APP_NAME,
                f"“{meeting.title}” is deleted, but some of its files couldn't be removed (still open elsewhere?):\n\n"
                + "\n".join(str(path) for path in left_behind),
            )
        else:
            self.app.toast(f"Deleted “{meeting.title}”")

    def _show_banner(self, text: str, *, retry: bool) -> None:
        self._banner_label.configure(text=text)
        if retry:
            self.retry_button.configure(state="normal")
            self.retry_button.pack(side="right", padx=(10, 0), before=self._banner_label)
        else:
            self.retry_button.pack_forget()
        self._banner.pack(fill="x", after=self._detail_top)

    def _transcribe_again(self, engine: str) -> None:
        """Transcribes the selected finished meeting's recording again, on this PC or in the cloud, in the
        background — replacing that way's transcript if it already has one."""
        meeting = self._current
        if meeting is None:
            return
        where = "on this PC" if engine == LOCAL_ENGINE else "in the cloud"
        widget = self.local_text if engine == LOCAL_ENGINE else self.cloud_text
        already = _meeting_transcripts(self.app.db.get_segments(meeting.id))[1 if engine == LOCAL_ENGINE else 2]
        if already and not messagebox.askyesno(
            APP_NAME, f"“{meeting.title}” already has a transcript made {where}. Replace it with a new one?"
        ):
            return
        if engine == CLOUD_ENGINE and not (self.app.settings.runpod_api_key and self.app.settings.runpod_endpoint_id):
            messagebox.showerror(APP_NAME, "Set the Runpod API key and endpoint ID in Settings first.")
            return
        for button in self._transcribe_buttons.values():
            button.configure(state="disabled")
        self._show_banner(f"Transcribing “{meeting.title}” {where}…", retry=False)
        self.detail_notebook.select(widget.master)
        settings = self.app.settings

        def report(message: str) -> None:
            self.after(0, self._show_retry_progress, meeting.id, message)

        def worker() -> None:
            try:
                add_transcription(settings, self.app.db, meeting.id, engine, on_progress=report)
            except Exception as exc:
                self.after(0, self._on_transcribe_again_failed, meeting.id, meeting.title, where, exc)
                return
            self.after(0, self._on_transcribe_again_done, meeting.id, meeting.title, where)

        self.app.run_in_background(worker)

    def _on_transcribe_again_done(self, meeting_id: int, meeting_title: str, where: str) -> None:
        if self._still_viewing(meeting_id):
            self._show_meeting(self._current)
        self.app.toast(f"“{meeting_title}” transcribed {where}")

    def _on_transcribe_again_failed(self, meeting_id: int, meeting_title: str, where: str, exc: Exception) -> None:
        if self._still_viewing(meeting_id):
            self._show_meeting(self._current)
        messagebox.showerror(APP_NAME, f'Couldn\'t transcribe "{meeting_title}" {where}: {exc}')

    def _retry_transcription(self) -> None:
        """Re-runs transcription for the selected unfinished meeting in the background."""
        meeting = self._current
        if meeting is None:
            return
        self.retry_button.configure(state="disabled")
        self._banner_label.configure(text=f"Retrying transcription for “{meeting.title}”…")

        def report(message: str) -> None:
            self.after(0, self._show_retry_progress, meeting.id, message)

        def worker() -> None:
            try:
                retry_meeting_transcription(self.app.settings, self.app.db, meeting.id, on_progress=report)
            except Exception as exc:
                self.after(0, self._on_retry_failed, meeting.id, meeting.title, exc)
                return
            self.after(0, self._on_retry_done, meeting.id, meeting.title)

        self.app.run_in_background(worker)

    def _still_viewing(self, meeting_id: int) -> bool:
        return self._current is not None and self._current.id == meeting_id

    def _show_retry_progress(self, meeting_id: int, message: str) -> None:
        if self._still_viewing(meeting_id):
            self._banner_label.configure(text=message)

    def _on_retry_done(self, meeting_id: int, meeting_title: str) -> None:
        self.refresh()
        self.app.toast(f"“{meeting_title}” transcribed")

    def _on_retry_failed(self, meeting_id: int, meeting_title: str, exc: Exception) -> None:
        if self._still_viewing(meeting_id):
            self._banner_label.configure(text=_UNFINISHED_MEETING_NOTICE)
            self.retry_button.configure(state="normal")
        messagebox.showerror(APP_NAME, f'Couldn\'t retry "{meeting_title}": {exc}')


# --- Settings -------------------------------------------------------------------------------------


class SettingsPage(ttk.Frame):
    """Every preference, grouped. Nothing applies until Save; saved settings are written to settings.json
    and take effect immediately (transcription choices from the next meeting on)."""

    def __init__(self, parent: tk.Misc, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        settings = app.settings
        scroller = _ScrollableFrame(self, app.palette)
        scroller.pack(fill="both", expand=True)
        page = scroller.inner
        ttk.Label(page, text="Settings", style="Heading.TLabel").pack(anchor="w", pady=(0, 16))

        # Audio
        body = self._section(page, "Audio")
        self.auto_switch_var = tk.BooleanVar(value=settings.auto_switch_audio_devices)
        ttk.Checkbutton(
            body, text="Switch devices automatically", variable=self.auto_switch_var, style="Card.TCheckbutton"
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self._hint(
            body, 1,
            "Records the microphone and speaker Teams is using. When that can't be told, follows whichever "
            "speaker is playing and uses a headset mic when one is live. Picking a device on the Record page "
            "mid-meeting turns this off for that meeting.",
        )
        self.headset_var = tk.StringVar(value=settings.headset_microphone_name or AUTO_HEADSET_LABEL)
        self.headset_combo = self._field(body, 2, "Headset microphone", ttk.Combobox(
            body, textvariable=self.headset_var, state="readonly", width=40
        ))
        self._hint(body, 3, "Your usual headset, tried first — or recognize any input Windows calls a headset.")

        # Transcription
        body = self._section(page, "Transcription")
        self.whisper_model_var = tk.StringVar(value=settings.whisper_model_size)
        self._field(body, 0, "Model", ttk.Combobox(
            body, textvariable=self.whisper_model_var, values=WHISPER_MODEL_SIZES, state="readonly", width=20
        ))
        self._hint(
            body, 1,
            "Larger models are more accurate but slower and need much more memory — a long meeting can run out "
            "of memory on medium/large. A size not used before is downloaded on first use.",
        )
        # Applied as soon as they're ticked, not on Save: they decide what happens to the meeting being
        # recorded when it stops, and an unsaved untick used to leave it going to the cloud anyway.
        self.transcribe_locally_var = tk.BooleanVar(value=settings.transcribe_locally)
        self.transcribe_in_cloud_var = tk.BooleanVar(value=settings.transcribe_in_cloud)
        ttk.Checkbutton(
            body, text="Transcribe on this PC", variable=self.transcribe_locally_var,
            style="Card.TCheckbutton", command=self._on_transcription_choice,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Checkbutton(
            body, text="Transcribe in the cloud with Runpod — names the other side's speakers, and sends the "
            "meeting's audio (both sides) to the cloud",
            variable=self.transcribe_in_cloud_var, style="Card.TCheckbutton", command=self._on_transcription_choice,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._hint(
            body, 4,
            "At least one has to stay ticked. Tick both to keep both transcripts and compare them in the Library, "
            "where either can also be made later from a meeting's recording. Takes effect straight away, "
            "including for a meeting being recorded now.",
        )
        self._runpod = ttk.Frame(body, style="Card.TFrame", borderwidth=0)
        self._runpod.grid(row=5, column=0, columnspan=2, sticky="we")
        self._runpod.columnconfigure(1, weight=1)
        self.runpod_api_key_var = tk.StringVar(value=settings.runpod_api_key or "")
        self.runpod_endpoint_id_var = tk.StringVar(value=settings.runpod_endpoint_id or "")
        self.runpod_hf_token_var = tk.StringVar(value=settings.runpod_huggingface_token or "")
        self._field(self._runpod, 0, "API key", ttk.Entry(self._runpod, textvariable=self.runpod_api_key_var, show="•"))
        self._field(self._runpod, 1, "Endpoint ID", ttk.Entry(self._runpod, textvariable=self.runpod_endpoint_id_var))
        self._field(
            self._runpod, 2, "HuggingFace token", ttk.Entry(self._runpod, textvariable=self.runpod_hf_token_var, show="•")
        )
        self._hint(
            self._runpod, 3,
            "From Runpod's Serverless dashboard. The HuggingFace token can stay blank if it's set on the "
            "endpoint. If Runpod fails and this PC isn't transcribing too, the audio is transcribed on this "
            "PC instead.",
        )

        # Copilot Studio
        body = self._section(page, "Copilot Studio handoff")
        folder_row = ttk.Frame(body, style="Card.TFrame", borderwidth=0)
        self.sync_dir_var = tk.StringVar(value=str(settings.copilot_sync_dir) if settings.copilot_sync_dir else "")
        ttk.Entry(folder_row, textvariable=self.sync_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="Browse…", command=self._browse_sync_dir).pack(side="left", padx=(8, 0))
        self._field(body, 0, "Sync folder", folder_row)
        self._hint(
            body, 1,
            "A folder OneDrive or SharePoint syncs. Each finished meeting is dropped into its Inbox subfolder "
            "for Copilot Studio to pick up. Leave blank to keep meetings on this PC only.",
        )

        # Shortcuts
        body = self._section(page, "Keyboard shortcuts")
        self._pending_hotkeys: dict[str, HotkeyCombo | None] = {
            "start": settings.start_meeting_hotkey, "stop": settings.stop_meeting_hotkey,
        }
        self._capturing_hotkey: str | None = None
        self._hotkey_vars: dict[str, tk.StringVar] = {}
        self._hotkey_buttons: dict[str, ttk.Button] = {}
        for row, (which, label) in enumerate((("start", "Start recording"), ("stop", "Stop recording"))):
            line = ttk.Frame(body, style="Card.TFrame", borderwidth=0)
            var = tk.StringVar(value=_hotkey_label(self._pending_hotkeys[which]))
            ttk.Label(line, textvariable=var, style="Card.TLabel", width=22).pack(side="left")
            button = ttk.Button(line, text="Change…", command=lambda w=which: self._begin_hotkey_capture(w))
            button.pack(side="left")
            ttk.Button(line, text="Clear", style="Link.TButton", command=lambda w=which: self._clear_hotkey(w)).pack(
                side="left", padx=(6, 0)
            )
            self._field(body, row, label, line)
            self._hotkey_vars[which], self._hotkey_buttons[which] = var, button
        self._hint(
            body, 2,
            "Work system-wide, even while Teams has focus. Press a combo with Ctrl, Alt, Shift or Win; Esc cancels. "
            "Also in the app: Ctrl+T stamps the time in your notes, Ctrl+F searches the Library.",
        )

        # Storage
        body = self._section(page, "Storage")
        real_data_dir = os.path.realpath(settings.data_dir)
        line = ttk.Frame(body, style="Card.TFrame", borderwidth=0)
        ttk.Label(line, text=real_data_dir, style="Card.TLabel").pack(side="left")
        ttk.Button(line, text="Open", style="Link.TButton", command=self._open_data_folder).pack(
            side="left", padx=(10, 0)
        )
        self._field(body, 0, "Data folder", line)
        if _is_store_python_private(real_data_dir):
            self._hint(
                body, 1,
                "This is a private folder of the Microsoft Store Python, which Windows deletes if that Python is "
                f"uninstalled. Meeting Scribe tries to move it to {default_data_dir()} each time it starts — close "
                "every copy of the app and start it again. If this stays, check that folder is empty or missing.",
            )

        footer = ttk.Frame(page)
        footer.pack(fill="x", pady=(18, 0))
        ttk.Button(footer, text="Save settings", style="Accent.TButton", command=self._save).pack(side="left")

        self.apply_device_lists(_enumerate_devices()[0])

    # -- layout helpers -------------------------------------------------------------------------------

    def _section(self, page: ttk.Frame, title: str) -> ttk.Frame:
        card, body = _card(page, title)
        card.pack(fill="x", pady=(0, 14))
        inner = ttk.Frame(body, style="Card.TFrame", borderwidth=0)
        inner.pack(fill="x")
        inner.columnconfigure(1, weight=1)
        return inner

    def _field(self, parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> tk.Widget:
        ttk.Label(parent, text=label, style="Card.TLabel", width=18).grid(row=row, column=0, sticky="w", pady=(10, 0))
        widget.grid(row=row, column=1, sticky="we", pady=(10, 0))
        return widget

    def _hint(self, parent: ttk.Frame, row: int, text: str) -> None:
        ttk.Label(parent, text=text, style="Card.Muted.TLabel", wraplength=640, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

    def _on_transcription_choice(self) -> None:
        local, cloud = self.transcribe_locally_var.get(), self.transcribe_in_cloud_var.get()
        if not (local or cloud):
            # The last one can't be unticked: a meeting would be recorded and never transcribed.
            self.transcribe_locally_var.set(self.app.settings.transcribe_locally)
            self.transcribe_in_cloud_var.set(self.app.settings.transcribe_in_cloud)
            self.app.toast("At least one way of transcribing has to stay ticked")
            return
        self.app.settings = update_settings(self.app.settings, transcribe_locally=local, transcribe_in_cloud=cloud)
        self.app.toast("Transcription setting saved")

    def apply_device_lists(self, input_devices) -> None:
        self.headset_combo["values"] = _device_choices(
            [d.name for d in input_devices], self.headset_var.get(), default_label=AUTO_HEADSET_LABEL
        )

    def _browse_sync_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a OneDrive/SharePoint-synced folder")
        if chosen:
            self.sync_dir_var.set(chosen)

    # -- shortcuts --------------------------------------------------------------------------------

    def _begin_hotkey_capture(self, which: str) -> None:
        """Listens for the next key combo pressed in the app, to assign as a shortcut."""
        if self._capturing_hotkey is not None:
            return
        self._capturing_hotkey = which
        self._hotkey_vars[which].set("Press keys…  (Esc cancels)")
        self._hotkey_buttons[which].configure(state="disabled")
        self.bind_all("<KeyPress>", self._on_hotkey_capture_keypress)

    def _on_hotkey_capture_keypress(self, event: "tk.Event") -> None:
        if event.keysym == "Escape":
            self._end_hotkey_capture(None, cancelled=True)
            return
        if is_modifier_keysym(event.keysym) or key_name(event.keycode) is None:
            return  # a modifier on its own, or an unsupported key — keep listening
        modifiers = current_modifiers()
        if modifiers == 0:
            return  # a global shortcut needs at least one modifier held
        self._end_hotkey_capture(HotkeyCombo(modifiers=modifiers, vk=event.keycode), cancelled=False)

    def _end_hotkey_capture(self, combo: HotkeyCombo | None, *, cancelled: bool) -> None:
        which, self._capturing_hotkey = self._capturing_hotkey, None
        self.unbind_all("<KeyPress>")
        if which is None:
            return
        if not cancelled:
            self._pending_hotkeys[which] = combo
        self._hotkey_vars[which].set(_hotkey_label(self._pending_hotkeys[which]))
        self._hotkey_buttons[which].configure(state="normal")

    def _clear_hotkey(self, which: str) -> None:
        self._pending_hotkeys[which] = None
        self._hotkey_vars[which].set(_hotkey_label(None))

    # -- save -------------------------------------------------------------------------------------------

    def _save(self) -> None:
        start, stop = self._pending_hotkeys["start"], self._pending_hotkeys["stop"]
        if start is not None and start == stop:
            messagebox.showerror(APP_NAME, "Start and Stop can't use the same shortcut.")
            return
        hotkeys_changed = (start, stop) != (
            self.app.settings.start_meeting_hotkey, self.app.settings.stop_meeting_hotkey
        )
        self.app.settings = update_settings(
            self.app.settings,
            copilot_sync_dir=self.sync_dir_var.get().strip(),
            auto_switch_audio_devices=self.auto_switch_var.get(),
            headset_microphone_name=_device_from_choice(self.headset_var.get(), AUTO_HEADSET_LABEL),
            whisper_model_size=self.whisper_model_var.get(),
            transcribe_locally=self.transcribe_locally_var.get(),
            transcribe_in_cloud=self.transcribe_in_cloud_var.get(),
            runpod_api_key=self.runpod_api_key_var.get().strip(),
            runpod_endpoint_id=self.runpod_endpoint_id_var.get().strip(),
            runpod_huggingface_token=self.runpod_hf_token_var.get().strip(),
            start_meeting_hotkey=start,
            stop_meeting_hotkey=stop,
        )
        if hotkeys_changed:
            self.app.apply_hotkeys()
        self.app.toast("Settings saved")

    def _open_data_folder(self) -> None:
        data_dir = self.app.settings.data_dir
        try:
            _open_path(data_dir)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't open {data_dir}: {exc}")
