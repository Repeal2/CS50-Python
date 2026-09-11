"""Tkinter control panel: start/stop meetings, browse past meetings per project, and upload context
documents.

Windows desktop only (ships with Python's standard install there). Recording and the finish-up work
(transcription, pushing to Copilot Studio, saving) run on a background thread so the UI doesn't freeze,
and so starting the next meeting doesn't have to wait for the previous one to finish (see session.py).
"""

from __future__ import annotations

import re
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from meeting_scribe.config import (
    Settings,
    load_settings,
    update_audio_devices,
    update_copilot_settings,
)
from meeting_scribe.screen.capture import ocr_region
from meeting_scribe.screen.region_picker import (
    RegionOutline,
    RegionTarget,
    WindowRegionTarget,
    pick_region_interactively,
    pin_region_to_window,
)
from meeting_scribe.session import MeetingSession
from meeting_scribe.storage.database import Database
from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text, save_original_copy
from meeting_scribe.transcription.engine import TranscriptLine, render_transcript

# Shown in a device dropdown in place of a device name, meaning "follow whatever Windows currently
# considers the default" rather than a specific device — used on both the Record tab and Settings tab.
SYSTEM_DEFAULT_LABEL = "System default"

# Manual-notes editor: matches leading whitespace plus an optional bullet marker ("-", "*", or "•")
# followed by a space, so Enter can continue the same bullet/indent on the next line. A plain line (no
# bullet) just matches its leading whitespace, so it stays plain rather than getting a bullet forced on.
_NOTES_BULLET_PREFIX = re.compile(r"^[ \t]*(?:[-*•]\s+)?")
_NOTES_INDENT = "    "


def _format_meeting_timestamp(iso_string: str) -> str:
    """Renders a stored UTC ISO timestamp (e.g. "2026-07-25T12:17:32.123456+00:00") in the user's local
    time and a human-readable format, instead of the raw string."""
    try:
        return datetime.fromisoformat(iso_string).astimezone().strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return iso_string


def _add_scrollable_text_tab(notebook: ttk.Notebook, title: str) -> tk.Text:
    """Adds a read-only-by-convention Text+Scrollbar pair as a new tab and returns the Text widget."""
    frame = ttk.Frame(notebook)
    notebook.add(frame, text=title)
    text_widget = tk.Text(frame, wrap="word")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
    text_widget.configure(yscrollcommand=scrollbar.set)
    text_widget.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    return text_widget


def _set_text(widget: tk.Text, content: str) -> None:
    widget.delete("1.0", "end")
    widget.insert("1.0", content)


def _enable_per_monitor_dpi_awareness() -> None:
    """Marks this process per-monitor DPI aware, so Windows reports actual physical-pixel window and
    monitor coordinates instead of scaling them to match whatever DPI setting the *primary* monitor
    happens to use. Without this, a DPI-unaware process gets coordinates that Windows silently
    virtualizes for it on any other monitor with a different scale factor — which would throw off
    win32gui.GetWindowRect (window_picker) and mss's screen capture, and make this app's own picker
    overlay and region outline render blurry or the wrong size/position on such a monitor. Must run
    before any window is created, including this Tk root, which is why MeetingScribeApp.__init__ calls
    it before super().__init__(). A failure here (e.g. shcore.dll missing on a very old Windows version)
    just leaves the process at its default awareness rather than crashing the app over it."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # coarser system-DPI-aware fallback (Vista+)
        except (AttributeError, OSError):
            pass  # very old Windows with neither API — just runs DPI-unaware, as it always did


class MeetingScribeApp(tk.Tk):
    def __init__(self, settings: Settings | None = None):
        _enable_per_monitor_dpi_awareness()
        super().__init__()
        self.title("Meeting Scribe")
        self.geometry("980x680")

        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        self._session: MeetingSession | None = None
        # A windowed PyInstaller build has no console, so an exception raised inside any Tkinter
        # callback (a button command, an after() callback) would otherwise just vanish with nothing to
        # show for it. Tkinter calls this instead of its default stderr-print behavior — log it to a
        # file next to the database so there's at least something to look at if the app misbehaves.
        self.report_callback_exception = self._report_callback_exception

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self._record_tab = RecordTab(notebook, self)
        self._projects_tab = ProjectsTab(notebook, self)
        self._settings_tab = SettingsTab(notebook, self)
        notebook.add(self._record_tab, text="Record Meeting")
        notebook.add(self._projects_tab, text="Projects & Search")
        notebook.add(self._settings_tab, text="Settings")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def refresh_project_lists(self) -> None:
        self._record_tab.refresh_projects()
        self._projects_tab.refresh_projects()

    def sync_device_displays(self) -> None:
        """The mic/system audio choice can be changed from either the Record tab or the Settings tab —
        whichever one didn't make the change calls this so it doesn't keep showing a stale value."""
        self._record_tab.sync_from_settings()
        self._settings_tab.sync_from_settings()

    def prompt_new_project(self) -> None:
        """Opens a small dialog to create a project by name, then refreshes both tabs and selects the
        new project in whichever one triggered this, so it's immediately usable without hunting for it
        in the list."""
        name = simpledialog.askstring("New Project", "Project name:", parent=self)
        if not name or not name.strip():
            return
        project = self.db.get_or_create_project(name.strip())
        self.refresh_project_lists()
        self._record_tab.select_project(project.name)
        self._projects_tab.select_project(project.name)

    def _on_close(self) -> None:
        self._record_tab.close_region_outline()
        self.db.close()
        self.destroy()

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        try:
            log_path = self.settings.data_dir / "error.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
        except OSError:
            pass  # logging the error shouldn't itself be able to crash the app


class RecordTab(ttk.Frame):
    ENTIRE_SCREEN_LABEL = "Entire screen"
    PIN_NONE_LABEL = "(fixed position)"

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._window_targets: list = []
        self._region_target: RegionTarget | WindowRegionTarget | None = None
        self._region_outline: RegionOutline | None = None
        self._input_devices: list = []
        self._loopback_devices: list = []
        # Which session's capture errors have already been written to the activity log, and how many of
        # them — see _log_new_capture_errors.
        self._capture_error_session = None
        self._capture_errors_logged = 0

        form = ttk.Frame(self)
        form.pack(fill="x", padx=12, pady=12)

        ttk.Label(form, text="Project").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(form, textvariable=self.project_var, width=40)
        self.project_combo.grid(row=0, column=1, sticky="we", padx=6, pady=4)
        self.project_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_title_suggestions())
        self.project_combo.bind("<FocusOut>", lambda _e: self._refresh_title_suggestions())
        ttk.Button(form, text="Add Project", command=self.app.prompt_new_project).grid(
            row=0, column=2, padx=(6, 0)
        )

        ttk.Label(form, text="Meeting title").grid(row=1, column=0, sticky="w")
        self.title_var = tk.StringVar(value="Untitled meeting")
        # Editable (not state="readonly"), so it still doubles as a free-typed title field — the
        # dropdown values are just suggestions of titles already used in this project, for quick repeat
        # meetings ("Weekly Client Meeting") rather than retyping the same title every time.
        self.title_combo = ttk.Combobox(form, textvariable=self.title_var, width=40)
        self.title_combo.grid(row=1, column=1, sticky="we", padx=6, pady=4)
        self.title_var.trace_add("write", self._on_title_changed)

        ttk.Label(form, text="Screen source").grid(row=2, column=0, sticky="w")
        self.source_var = tk.StringVar(value=self.ENTIRE_SCREEN_LABEL)
        self.source_combo = ttk.Combobox(
            form, textvariable=self.source_var, width=40, state="readonly"
        )
        self.source_combo.grid(row=2, column=1, sticky="we", padx=6, pady=4)
        self.source_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_region_outline())
        source_buttons = ttk.Frame(form)
        source_buttons.grid(row=2, column=2, padx=(6, 0))
        ttk.Button(source_buttons, text="Refresh windows", command=self._refresh_windows).pack(
            side="left"
        )
        ttk.Button(source_buttons, text="Select area…", command=self._pick_region).pack(
            side="left", padx=(4, 0)
        )

        # "(fixed position)" keeps the area at whatever screen coordinates it was drawn at; picking a
        # window here instead pins it to that window's current bounds, so a later "Select area…" makes
        # a WindowRegionTarget (offset from the window's corner) rather than a plain RegionTarget — see
        # _pick_region. Moving/dragging that window afterwards, including to another monitor, moves the
        # captured area with it, the same way whole-window capture already does.
        ttk.Label(form, text="Pin area to window").grid(row=3, column=0, sticky="w")
        self.pin_window_var = tk.StringVar(value=self.PIN_NONE_LABEL)
        self.pin_window_combo = ttk.Combobox(
            form, textvariable=self.pin_window_var, width=40, state="readonly"
        )
        self.pin_window_combo.grid(row=3, column=1, sticky="we", padx=6, pady=4)

        ttk.Label(form, text="Microphone").grid(row=4, column=0, sticky="w")
        self.mic_var = tk.StringVar(value=self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.mic_combo = ttk.Combobox(form, textvariable=self.mic_var, width=40, state="readonly")
        self.mic_combo.grid(row=4, column=1, sticky="we", padx=6, pady=4)
        self.mic_combo.bind("<<ComboboxSelected>>", self._on_devices_changed)

        ttk.Label(form, text="System audio (speaker)").grid(row=5, column=0, sticky="w")
        self.system_var = tk.StringVar(
            value=self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL
        )
        self.system_combo = ttk.Combobox(form, textvariable=self.system_var, width=40, state="readonly")
        self.system_combo.grid(row=5, column=1, sticky="we", padx=6, pady=4)
        self.system_combo.bind("<<ComboboxSelected>>", self._on_devices_changed)

        ttk.Button(form, text="Refresh devices", command=self._refresh_devices).grid(
            row=4, column=2, rowspan=2, padx=(6, 0)
        )
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12)
        self.start_button = ttk.Button(buttons, text="Start Meeting", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop Meeting", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Upload Document…", command=self._upload_document).pack(
            side="left", padx=(0, 8)
        )
        self.capture_attendees_button = ttk.Button(
            buttons, text="Capture Attendees…", command=self._capture_attendees, state="disabled"
        )
        self.capture_attendees_button.pack(side="left")

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=12, pady=(8, 0))

        # Live input meters — the only way to actually confirm a device is picking up audio, rather
        # than just trusting that "Recording" means something is coming through.
        levels_frame = ttk.Frame(self)
        levels_frame.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(levels_frame, text="Mic level").grid(row=0, column=0, sticky="w")
        self.mic_level_bar = ttk.Progressbar(
            levels_frame, orient="horizontal", mode="determinate", maximum=100
        )
        self.mic_level_bar.grid(row=0, column=1, sticky="we", padx=(6, 0))
        ttk.Label(levels_frame, text="System level").grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.system_level_bar = ttk.Progressbar(
            levels_frame, orient="horizontal", mode="determinate", maximum=100
        )
        self.system_level_bar.grid(row=1, column=1, sticky="we", padx=(6, 0), pady=(2, 0))
        levels_frame.columnconfigure(1, weight=1)

        # Manual notes and the activity log split the remaining vertical space evenly: both frames below
        # use fill="both", expand=True inside the same parent, and Tkinter's pack geometry manager
        # divides leftover space equally between multiple expanding children packed the same way.
        notes_and_log = ttk.Frame(self)
        notes_and_log.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        notes_header = ttk.Frame(notes_and_log)
        notes_header.pack(fill="x")
        ttk.Label(notes_header, text="Manual notes").pack(side="left")
        ttk.Label(
            notes_header, text="Enter: continue bullet   ·   Tab / Shift+Tab: indent", foreground="#777"
        ).pack(side="right")

        notes_frame = ttk.Frame(notes_and_log)
        notes_frame.pack(fill="both", expand=True, pady=(2, 8))
        self.manual_notes_editor = tk.Text(notes_frame, wrap="word", state="disabled")
        notes_scrollbar = ttk.Scrollbar(
            notes_frame, orient="vertical", command=self.manual_notes_editor.yview
        )
        self.manual_notes_editor.configure(yscrollcommand=notes_scrollbar.set)
        self.manual_notes_editor.pack(side="left", fill="both", expand=True)
        notes_scrollbar.pack(side="right", fill="y")
        self.manual_notes_editor.bind("<KeyRelease>", self._schedule_manual_notes_save)
        self.manual_notes_editor.bind("<Return>", self._on_manual_notes_return)
        self.manual_notes_editor.bind("<Tab>", self._on_manual_notes_indent)
        self.manual_notes_editor.bind("<Shift-Tab>", self._on_manual_notes_dedent)
        self._manual_notes_save_after_id: str | None = None

        # A running activity log, not a transcript preview — the raw transcript/OCR text is already
        # long-form and belongs on the Projects & Search tab once a meeting is saved. This is just enough
        # to confirm at a glance that something is actually happening during the finish-up work.
        ttk.Label(notes_and_log, text="Activity log").pack(anchor="w")
        output_frame = ttk.Frame(notes_and_log)
        output_frame.pack(fill="both", expand=True, pady=(2, 0))
        self.output = tk.Text(output_frame, wrap="word", state="disabled")
        output_scrollbar = ttk.Scrollbar(output_frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scrollbar.set)
        self.output.pack(side="left", fill="both", expand=True)
        output_scrollbar.pack(side="right", fill="y")

        self.refresh_projects()
        self._refresh_windows()
        self._refresh_devices()
        self._poll_audio_levels()
        self._poll_region_outline()

    def refresh_projects(self) -> None:
        names = [p.name for p in self.app.db.list_projects()]
        self.project_combo["values"] = names

    def _refresh_title_suggestions(self) -> None:
        """Repopulates the meeting-title dropdown with titles already used in the currently-entered
        project, most-recently-used first, so a recurring meeting ("Weekly Client Meeting") can be
        picked instead of retyped. Doesn't touch the current title itself, editable field, that value
        stays whatever the user typed or last selected."""
        project = self.app.db.get_project_by_name(self.project_var.get().strip())
        self.title_combo["values"] = (
            self.app.db.list_recent_meeting_titles(project.id) if project is not None else []
        )

    def _on_title_changed(self, *_args) -> None:
        """Keeps the meeting title editable for the whole life of a meeting, not just fixed at Start —
        persists to the DB (and the in-memory session used for the eventual Copilot push) on every
        change. Titles are short and edited infrequently compared to the manual notes box, so this
        skips the debouncing used there and just saves directly."""
        session = self.app._session
        if session is None:
            return
        new_title = self.title_var.get()
        session.title = new_title
        self.app.db.update_meeting_title(session.meeting_id, new_title)

    def _poll_audio_levels(self) -> None:
        """Runs continuously (not just while recording) so the meters are always current — cheap enough
        (two float reads, ~7x/sec) that there's no need to start/stop it."""
        session = self.app._session
        mic_level, system_level = session.audio_levels() if session is not None else (0.0, 0.0)
        self.mic_level_bar["value"] = mic_level * 100
        self.system_level_bar["value"] = system_level * 100
        if session is not None:
            self._log_new_capture_errors(session)
        self.after(150, self._poll_audio_levels)

    def _log_new_capture_errors(self, session) -> None:
        """Reports a capture stream that has died mid-meeting, once each, as it happens.

        The level meters alone can't tell this story: a microphone whose capture thread has fallen over
        reads exactly like a microphone nobody is talking into. Saying it out loud in the activity log
        is the difference between noticing now — while the meeting could still be restarted — and
        finding out when the transcript comes back with one side of the conversation missing."""
        if session is not self._capture_error_session:
            self._capture_error_session = session
            self._capture_errors_logged = 0
        errors = session.capture_errors()
        for message in errors[self._capture_errors_logged:]:
            self._log(f"[{session.title}] {message}")
        self._capture_errors_logged = len(errors)

    def _poll_region_outline(self) -> None:
        """Keeps a window-pinned custom area's on-screen outline glued to its window as the window
        moves — including across monitors — by re-reading the window's current bounds on a timer, the
        same idea as _poll_audio_levels. A plain (unpinned) area never moves, so there's nothing to do
        for it here; _sync_region_outline positions it once, when it's picked or selected."""
        if self._region_outline is not None and isinstance(self._region_target, WindowRegionTarget):
            rect = self._current_absolute_rect(self._region_target)
            if rect is not None:
                self._region_outline.reposition(rect)
        self.after(400, self._poll_region_outline)

    def select_project(self, name: str) -> None:
        self.project_var.set(name)

    def sync_from_settings(self) -> None:
        """Reflects a device change made elsewhere (e.g. the Settings tab) so this tab's dropdowns
        don't show a stale value for the same setting."""
        self.mic_var.set(self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.system_var.set(self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL)

    def _refresh_windows(self) -> None:
        """Repopulates the screen-source dropdown with currently open, titled windows the user can
        pick as an OCR target instead of the whole screen (e.g. just the Teams/Zoom window). Keeps
        whatever custom area is currently picked (if any) as an option alongside them. Also repopulates
        the "pin area to window" dropdown from the same window list, so a window that's closed since the
        last refresh disappears from both."""
        from meeting_scribe.screen.window_picker import list_capturable_windows

        try:
            self._window_targets = list_capturable_windows()
        except RuntimeError:
            self._window_targets = []  # not on Windows (e.g. dev machine) — whole-screen only
        region_labels = [self._region_target.label] if self._region_target else []
        values = [self.ENTIRE_SCREEN_LABEL] + region_labels + [w.title for w in self._window_targets]
        self.source_combo["values"] = values
        if self.source_var.get() not in values:
            self.source_var.set(self.ENTIRE_SCREEN_LABEL)

        pin_values = [self.PIN_NONE_LABEL] + [w.title for w in self._window_targets]
        self.pin_window_combo["values"] = pin_values
        if self.pin_window_var.get() not in pin_values:
            self.pin_window_var.set(self.PIN_NONE_LABEL)
        self._sync_region_outline()

    def _pick_region(self) -> None:
        """Opens the drag-to-select overlay, and if the user completes a selection, adds it to the
        screen-source dropdown as a new option and switches to it immediately. If a window is chosen in
        the "pin area to window" dropdown, the drawn rectangle is converted to an offset from that
        window's current position (WindowRegionTarget) instead of a fixed screen rectangle, so it keeps
        capturing the same part of the window even after the window moves — including to another
        monitor. A window that's closed/minimized right when "Select area…" is clicked has no current
        position to measure the offset from, so that case falls back to a fixed-position area, same as
        leaving the pin dropdown on "(fixed position)"."""
        region = pick_region_interactively(self)
        if region is None:
            return

        window = next((w for w in self._window_targets if w.title == self.pin_window_var.get()), None)
        if window is not None:
            from meeting_scribe.screen.window_picker import get_window_region

            window_rect = get_window_region(window.hwnd)
            if window_rect is not None:
                region = pin_region_to_window(region, window, window_rect)

        self._region_target = region
        self._refresh_windows()
        self.source_var.set(region.label)
        self._sync_region_outline()

    def _current_absolute_rect(self, target: RegionTarget | WindowRegionTarget) -> dict | None:
        """Resolves a region target to an mss-style {left, top, width, height} rect in current screen
        coordinates — a fixed RegionTarget already is one; a WindowRegionTarget needs its pinned
        window's current bounds re-read first. None if a pinned window has since closed or minimized,
        in which case there's nowhere on screen to draw the outline right now."""
        if isinstance(target, RegionTarget):
            return target.mss_region
        from meeting_scribe.screen.window_picker import get_window_region

        window_rect = get_window_region(target.hwnd)
        return None if window_rect is None else target.mss_region(window_rect)

    def _sync_region_outline(self) -> None:
        """Shows a live boundary around the custom area on screen for as long as it's the selected
        screen source — not just at the moment it was drawn — so it's always obvious what's being
        captured, the same idea as the audio level meters. Hides it the moment something else is
        picked instead. For a window-pinned area this only sets the *initial* position;
        _poll_region_outline keeps it glued to the window's current position afterwards."""
        if self._region_outline is not None:
            self._region_outline.close()
            self._region_outline = None
        if self._region_target is not None and self.source_var.get() == self._region_target.label:
            rect = self._current_absolute_rect(self._region_target)
            if rect is not None:
                self._region_outline = RegionOutline(self, rect)

    def close_region_outline(self) -> None:
        """Called when a meeting stops (nothing is being captured anymore, so the boundary shouldn't
        linger on screen) and on app shutdown (so the boundary windows don't outlive the main window).
        _start() calls _sync_region_outline() again, which re-shows it if the same area is still
        selected for the next meeting."""
        if self._region_outline is not None:
            self._region_outline.close()
            self._region_outline = None

    def _selected_screen_target(self):
        selected = self.source_var.get()
        if selected == self.ENTIRE_SCREEN_LABEL:
            return None
        if self._region_target is not None and selected == self._region_target.label:
            return self._region_target
        return next((w for w in self._window_targets if w.title == selected), None)

    def _refresh_devices(self) -> None:
        """Repopulates the microphone/system-audio dropdowns with currently available devices — the
        same picker as the Settings tab, surfaced here too so switching devices doesn't require leaving
        the Record tab."""
        from meeting_scribe.audio.device_picker import list_input_devices, list_loopback_devices

        try:
            self._input_devices = list_input_devices()
        except RuntimeError:
            self._input_devices = []  # not on Windows (e.g. dev machine)
        try:
            self._loopback_devices = list_loopback_devices()
        except RuntimeError:
            self._loopback_devices = []

        mic_values = [SYSTEM_DEFAULT_LABEL] + [d.name for d in self._input_devices]
        system_values = [SYSTEM_DEFAULT_LABEL] + [d.name for d in self._loopback_devices]
        self.mic_combo["values"] = mic_values
        self.system_combo["values"] = system_values
        if self.mic_var.get() not in mic_values:
            self.mic_var.set(SYSTEM_DEFAULT_LABEL)
        if self.system_var.get() not in system_values:
            self.system_var.set(SYSTEM_DEFAULT_LABEL)

    def _on_devices_changed(self, _event=None) -> None:
        """Persists the mic/system choice immediately (rather than waiting for a Settings-tab Save) so
        it takes effect the next time the user hits Start Meeting."""
        mic_name = None if self.mic_var.get() == SYSTEM_DEFAULT_LABEL else self.mic_var.get()
        system_name = None if self.system_var.get() == SYSTEM_DEFAULT_LABEL else self.system_var.get()
        self.app.settings = update_audio_devices(
            self.app.settings, mic_device_name=mic_name, system_device_name=system_name
        )
        self.app.sync_device_displays()

    def _log(self, message: str) -> None:
        """Appends a timestamped line to the activity log and scrolls to it. This is a log of what the
        app is doing (started, stopped, transcribing, waiting on notes...), not a transcript preview —
        the widget is otherwise kept disabled so it reads as a log rather than an editable text box."""
        timestamp = datetime.now().strftime("%b %d, %Y %I:%M:%S %p")
        self.output["state"] = "normal"
        self.output.insert("end", f"[{timestamp}] {message}\n")
        self.output["state"] = "disabled"
        self.output.see("end")

    def _start(self) -> None:
        project_name = self.project_var.get().strip()
        if not project_name:
            messagebox.showerror("Meeting Scribe", "Enter a project name first.")
            return
        meeting_title = self.title_var.get()
        try:
            session = MeetingSession(
                self.app.settings,
                self.app.db,
                project_name,
                meeting_title,
                screen_target=self._selected_screen_target(),
            )
            session.start()
        except RuntimeError as exc:
            messagebox.showerror("Meeting Scribe", str(exc))
            return

        self.app._session = session
        self.status_var.set(f"Recording — {project_name} / {meeting_title}")
        self.start_button["state"] = "disabled"
        self.stop_button["state"] = "normal"
        self.capture_attendees_button["state"] = "normal"

        self.manual_notes_editor["state"] = "normal"
        self.manual_notes_editor.delete("1.0", "end")

        # Deliberately not cleared here: a previous meeting's background finish-up job (see _stop())
        # may still be logging its own progress, and wiping the log out from under it would lose that
        # context. The [title] prefix on every line is what keeps overlapping meetings' entries readable.
        self._log(f'[{meeting_title}] Recording started — Project: {project_name}.')
        self._sync_region_outline()

    def _schedule_manual_notes_save(self, _event=None) -> None:
        """Debounces saving to the DB so a fast typist doesn't trigger a write on every keystroke —
        restarts a short timer on each edit, so the save actually happens ~400ms after typing pauses."""
        if self._manual_notes_save_after_id is not None:
            self.after_cancel(self._manual_notes_save_after_id)
        self._manual_notes_save_after_id = self.after(400, self._save_manual_notes)

    def _save_manual_notes(self) -> None:
        self._manual_notes_save_after_id = None
        session = self.app._session
        if session is None:
            return
        content = self.manual_notes_editor.get("1.0", "end-1c")
        self.app.db.set_manual_notes(session.meeting_id, content)

    def _on_manual_notes_return(self, event) -> str:
        """Continues the current line's indentation and bullet marker (if any) onto the new line, so a
        bulleted/nested list keeps going with just Enter instead of retyping "- " (or however many
        levels of indent) every time. A plain line has no bullet to match, so it just stays plain.
        (The <KeyRelease> binding covers scheduling the save for this and the two handlers below —
        Tkinter fires it after any key, including Return/Tab, so there's no need to also call it here.)
        """
        widget = event.widget
        line_text = widget.get("insert linestart", "insert lineend")
        match = _NOTES_BULLET_PREFIX.match(line_text)
        prefix = match.group(0) if match else ""
        widget.insert("insert", "\n" + prefix)
        return "break"

    def _on_manual_notes_indent(self, event) -> str:
        """Tab indents the whole current line one level, wherever the cursor is in it — matches how
        bullet lists behave in most note-taking apps, rather than inserting a literal tab character."""
        event.widget.insert("insert linestart", _NOTES_INDENT)
        return "break"

    def _on_manual_notes_dedent(self, event) -> str:
        widget = event.widget
        line_start = widget.index("insert linestart")
        line_text = widget.get(line_start, "insert lineend")
        leading_spaces = len(line_text) - len(line_text.lstrip(" "))
        remove = min(leading_spaces, len(_NOTES_INDENT))
        if remove:
            widget.delete(line_start, f"{line_start}+{remove}c")
        return "break"

    def _stop(self) -> None:
        session = self.app._session
        if session is None:
            return
        # Flush any pending debounced save before detaching (see _schedule_manual_notes_save) so the
        # last few keystrokes before Stop aren't lost — _save_manual_notes reads self.app._session, so
        # this has to happen before that gets cleared below.
        if self._manual_notes_save_after_id is not None:
            self.after_cancel(self._manual_notes_save_after_id)
            self._manual_notes_save_after_id = None
        self._save_manual_notes()

        # Detach right away rather than waiting for the background job below to finish — transcribing
        # and pushing to Copilot Studio takes a moment, and there's no reason that should block starting
        # the next meeting. Everything the background job needs (the session, its title) is already
        # captured in this closure, so the app has no more use for the "current" session slot.
        self.app._session = None

        self.stop_button["state"] = "disabled"
        self.capture_attendees_button["state"] = "disabled"
        self.manual_notes_editor["state"] = "disabled"
        self.start_button["state"] = "normal"
        self.status_var.set(f'Idle — finishing "{session.title}" in the background.')
        self._log(f'[{session.title}] Stop requested — finishing in the background.')
        # The custom-area boundary is a "this is what's being captured" indicator — leaving it on screen
        # after recording stops is just a stray colored box with nothing behind it. _start() re-shows it
        # if the same area is still selected next time.
        self.close_region_outline()

        def worker() -> None:
            def report(message: str) -> None:
                self.after(0, self._log, f"[{session.title}] {message}")

            try:
                session.stop(on_progress=report)
            except Exception as exc:  # surfaced to the user regardless of cause
                self.after(0, self._on_stop_failed, session.title, exc)
                return
            self.after(0, self._on_stop_done, session.title)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_done(self, meeting_title: str) -> None:
        self._log(f"[{meeting_title}] Saved.")
        # Don't stomp on a newer status — a different meeting may already be recording by the time this
        # background job finishes, and its "Recording — ..." status shouldn't be overwritten by a
        # trailing "Saved." from an unrelated, earlier meeting.
        if self.app._session is None:
            self.status_var.set("Saved.")
        self.app.refresh_project_lists()

    def _on_stop_failed(self, meeting_title: str, exc: Exception) -> None:
        self._log(f"[{meeting_title}] Failed: {exc}")
        if self.app._session is None:
            self.status_var.set("Failed.")
        messagebox.showerror("Meeting Scribe", f'Couldn\'t finish "{meeting_title}": {exc}')

    def _upload_document(self) -> None:
        """Attaches a document to the current project — and, if a meeting is actively recording right
        now, to that specific meeting — without needing to switch to the Projects & Search tab and pick
        things from a list. Once a meeting is stopped it's no longer "current" even while it's still
        finishing up in the background (see _stop()), so a document uploaded in that window attaches at
        the project level rather than to the just-ended meeting."""
        project_name = self.project_var.get().strip()
        if not project_name:
            messagebox.showerror("Meeting Scribe", "Enter a project name first.")
            return
        path_str = filedialog.askopenfilename(
            title="Attach a document",
            filetypes=[
                ("Supported documents", "*.pdf *.docx *.txt *.md *.png *.jpg *.jpeg *.bmp *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            text = extract_text(path, tesseract_cmd=self.app.settings.tesseract_cmd)
        except UnsupportedDocumentError as exc:
            messagebox.showerror("Meeting Scribe", str(exc))
            return
        # Keeps a copy of the original bytes (under a synthetic name — see save_original_copy) so the
        # real file, not just its extracted text, is still around later for the Copilot push package,
        # which hands reference documents off under their real filename.
        source_path = save_original_copy(path, self.app.settings.documents_dir)

        project = self.app.db.get_or_create_project(project_name)
        session = self.app._session
        meeting_id = session.meeting_id if session is not None else None
        self.app.db.add_document(
            project.id, path.name, text, meeting_id=meeting_id, source_path=str(source_path)
        )
        self.app.refresh_project_lists()

        if meeting_id is not None:
            self._log(f"[{session.title}] Document attached: {path.name}")
            messagebox.showinfo("Meeting Scribe", f'Added {path.name} to this meeting.')
        else:
            messagebox.showinfo("Meeting Scribe", f'Added {path.name} to project "{project.name}".')

    def _capture_attendees(self) -> None:
        """Lets the user drag out a rectangle over an attendee/participants panel and OCRs it
        immediately (not on a delay, and not part of the continuous screen watcher), appending the
        result to this meeting's attendee list. Only available while a meeting is actively recording,
        same as the Stop button, since there's no meeting to attach attendees to otherwise."""
        session = self.app._session
        if session is None:
            messagebox.showerror("Meeting Scribe", "Start a meeting first.")
            return
        region = pick_region_interactively(self)
        if region is None:
            return
        try:
            text = ocr_region(region.mss_region, tesseract_cmd=self.app.settings.tesseract_cmd)
        except Exception as exc:  # surfaced to the user regardless of cause
            messagebox.showerror("Meeting Scribe", f"Couldn't read that area: {exc}")
            return
        if not text.strip():
            messagebox.showinfo("Meeting Scribe", "No text found in that area.")
            return

        existing = self.app.db.get_meeting(session.meeting_id).attendees or ""
        combined = (existing + "\n" + text).strip() if existing else text.strip()
        self.app.db.set_attendees(session.meeting_id, combined)
        self._log(f'[{session.title}] Attendees captured ({len(text.strip().splitlines())} line(s)).')


class ProjectsTab(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app

        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=12, pady=12)

        projects_header = ttk.Frame(left)
        projects_header.pack(fill="x")
        ttk.Label(projects_header, text="Projects").pack(side="left")
        ttk.Button(projects_header, text="Add Project", command=self.app.prompt_new_project).pack(
            side="right"
        )

        # Uploading here (rather than only from the Record tab) is for documents that turn up after a
        # meeting is already over — a follow-up email, a shared recording transcript from elsewhere — so
        # it attaches to whichever meeting is currently selected, or to the project as a whole if none is.
        ttk.Button(left, text="Upload Document…", command=self._upload_document).pack(
            fill="x", pady=(8, 0)
        )

        project_list_frame = ttk.Frame(left)
        project_list_frame.pack(fill="both", expand=True)
        self.project_list = tk.Listbox(project_list_frame, width=36, height=10, exportselection=False)
        project_list_scrollbar = ttk.Scrollbar(
            project_list_frame, orient="vertical", command=self.project_list.yview
        )
        self.project_list.configure(yscrollcommand=project_list_scrollbar.set)
        self.project_list.pack(side="left", fill="both", expand=True)
        project_list_scrollbar.pack(side="right", fill="y")
        self.project_list.bind("<<ListboxSelect>>", self._on_project_selected)

        ttk.Label(left, text="Meetings").pack(anchor="w", pady=(12, 0))
        meeting_list_frame = ttk.Frame(left)
        meeting_list_frame.pack(fill="both", expand=True)
        self.meeting_list = tk.Listbox(meeting_list_frame, width=36, height=10, exportselection=False)
        meeting_list_scrollbar = ttk.Scrollbar(
            meeting_list_frame, orient="vertical", command=self.meeting_list.yview
        )
        self.meeting_list.configure(yscrollcommand=meeting_list_scrollbar.set)
        self.meeting_list.pack(side="left", fill="both", expand=True)
        meeting_list_scrollbar.pack(side="right", fill="y")
        self.meeting_list.bind("<<ListboxSelect>>", self._on_meeting_selected)

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=12, pady=12)

        # Selecting a meeting fans its details out across these tabs, rather than dumping everything
        # into one pane — the raw on-screen OCR log, the spoken-audio transcript, and the notes the user
        # typed themselves during the meeting are different things a user reaches for at different times.
        self.detail_notebook = ttk.Notebook(right)
        self.detail_notebook.pack(fill="both", expand=True)

        self.ocr_text = _add_scrollable_text_tab(self.detail_notebook, "OCR Transcript")
        self.audio_text = _add_scrollable_text_tab(self.detail_notebook, "Audio Transcript")
        self.manual_notes_text = _add_scrollable_text_tab(self.detail_notebook, "Manual notes")
        self.attendees_text = _add_scrollable_text_tab(self.detail_notebook, "Attendees")
        self._build_documents_tab()

        self._projects: list = []
        self._meetings: list = []
        self._meeting_documents: list = []
        self.refresh_projects()

    def _build_documents_tab(self) -> None:
        frame = ttk.Frame(self.detail_notebook)
        self.detail_notebook.add(frame, text="Additional Documents")

        docs_list_frame = ttk.Frame(frame)
        docs_list_frame.pack(side="left", fill="y", padx=(0, 8))
        self.meeting_documents_list = tk.Listbox(
            docs_list_frame, width=24, height=14, exportselection=False
        )
        docs_list_scrollbar = ttk.Scrollbar(
            docs_list_frame, orient="vertical", command=self.meeting_documents_list.yview
        )
        self.meeting_documents_list.configure(yscrollcommand=docs_list_scrollbar.set)
        self.meeting_documents_list.pack(side="left", fill="both", expand=True)
        docs_list_scrollbar.pack(side="right", fill="y")
        self.meeting_documents_list.bind("<<ListboxSelect>>", self._on_meeting_document_selected)

        docs_viewer_frame = ttk.Frame(frame)
        docs_viewer_frame.pack(side="left", fill="both", expand=True)
        self.meeting_document_viewer = tk.Text(docs_viewer_frame, wrap="word")
        docs_viewer_scrollbar = ttk.Scrollbar(
            docs_viewer_frame, orient="vertical", command=self.meeting_document_viewer.yview
        )
        self.meeting_document_viewer.configure(yscrollcommand=docs_viewer_scrollbar.set)
        self.meeting_document_viewer.pack(side="left", fill="both", expand=True)
        docs_viewer_scrollbar.pack(side="right", fill="y")

    def refresh_projects(self) -> None:
        self._projects = self.app.db.list_projects()
        self.project_list.delete(0, "end")
        for project in self._projects:
            self.project_list.insert("end", project.name)

    def _selected_project(self):
        selection = self.project_list.curselection()
        if not selection:
            return None
        return self._projects[selection[0]]

    def _selected_meeting(self):
        selection = self.meeting_list.curselection()
        if not selection:
            return None
        return self._meetings[selection[0]]

    def select_project(self, name: str) -> None:
        for index, project in enumerate(self._projects):
            if project.name == name:
                self.project_list.selection_clear(0, "end")
                self.project_list.selection_set(index)
                self.project_list.see(index)
                self._on_project_selected(None)
                return

    def _upload_document(self) -> None:
        """Attaches a document after the fact: to whichever meeting is currently selected in the list,
        or to the project itself if none is selected. This is the post-meeting counterpart to the
        Record tab's own upload button, which only ever attaches to the meeting in progress right now."""
        project = self._selected_project()
        if project is None:
            messagebox.showerror("Meeting Scribe", "Select a project first.")
            return
        path_str = filedialog.askopenfilename(
            title="Attach a document",
            filetypes=[
                ("Supported documents", "*.pdf *.docx *.txt *.md *.png *.jpg *.jpeg *.bmp *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            text = extract_text(path, tesseract_cmd=self.app.settings.tesseract_cmd)
        except UnsupportedDocumentError as exc:
            messagebox.showerror("Meeting Scribe", str(exc))
            return
        source_path = save_original_copy(path, self.app.settings.documents_dir)

        meeting = self._selected_meeting()
        meeting_id = meeting.id if meeting is not None else None
        self.app.db.add_document(
            project.id, path.name, text, meeting_id=meeting_id, source_path=str(source_path)
        )
        if meeting_id is not None:
            self._load_meeting_documents(meeting_id)

        target = f'meeting "{meeting.title}"' if meeting is not None else f'project "{project.name}"'
        messagebox.showinfo("Meeting Scribe", f"Added {path.name} to {target}.")

    def _on_project_selected(self, _event) -> None:
        project = self._selected_project()
        self.meeting_list.delete(0, "end")
        self._meetings = []
        self._clear_meeting_details()
        if project is None:
            return
        self._meetings = self.app.db.list_meetings(project.id)
        for meeting in self._meetings:
            timestamp = _format_meeting_timestamp(meeting.started_at)
            self.meeting_list.insert("end", f"{meeting.title} — {timestamp}")

    def _clear_meeting_details(self) -> None:
        for widget in (self.ocr_text, self.audio_text, self.manual_notes_text, self.attendees_text):
            _set_text(widget, "")
        self._meeting_documents = []
        self.meeting_documents_list.delete(0, "end")
        _set_text(self.meeting_document_viewer, "")

    def _on_meeting_selected(self, _event) -> None:
        meeting = self._selected_meeting()
        if meeting is None:
            return

        segments = self.app.db.get_segments(meeting.id)
        lines = [TranscriptLine(row["timestamp_seconds"], row["source"], row["text"]) for row in segments]
        ocr_lines = [line for line in lines if line.source == "screen_ocr"]
        audio_lines = [line for line in lines if line.source in ("mic", "system")]

        _set_text(self.ocr_text, render_transcript(ocr_lines) or "(no on-screen text captured)")
        _set_text(self.audio_text, render_transcript(audio_lines) or "(no speech captured)")
        _set_text(self.manual_notes_text, meeting.manual_notes or "(no manual notes for this meeting)")
        _set_text(self.attendees_text, meeting.attendees or "(no attendees captured)")

        self._load_meeting_documents(meeting.id)

    def _load_meeting_documents(self, meeting_id: int) -> None:
        self._meeting_documents = self.app.db.list_documents_for_meeting(meeting_id)
        self.meeting_documents_list.delete(0, "end")
        _set_text(self.meeting_document_viewer, "")
        if not self._meeting_documents:
            self.meeting_documents_list.insert("end", "(none uploaded)")
            return
        for document in self._meeting_documents:
            self.meeting_documents_list.insert("end", document["filename"])

    def _on_meeting_document_selected(self, _event) -> None:
        selection = self.meeting_documents_list.curselection()
        if not selection or not self._meeting_documents:
            return
        document = self._meeting_documents[selection[0]]
        _set_text(self.meeting_document_viewer, document["content_text"])


class SettingsTab(ttk.Frame):
    """Copilot push folder and audio device selection — all editable without touching environment
    variables. Saved settings are written to disk (see config.save_user_config) and take effect
    immediately for this running session."""

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._input_devices: list = []
        self._loopback_devices: list = []
        # Which session's capture errors have already been written to the activity log, and how many of
        # them — see _log_new_capture_errors.
        self._capture_error_session = None
        self._capture_errors_logged = 0

        form = ttk.Frame(self)
        form.pack(fill="x", padx=12, pady=12, anchor="n")

        ttk.Label(form, text="Copilot sync folder").grid(row=0, column=0, sticky="w")
        self.sync_dir_var = tk.StringVar(
            value=str(self.app.settings.copilot_sync_dir) if self.app.settings.copilot_sync_dir else ""
        )
        ttk.Entry(form, textvariable=self.sync_dir_var, width=48).grid(
            row=0, column=1, sticky="we", padx=6, pady=4
        )
        ttk.Button(form, text="Browse…", command=self._browse_sync_dir).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(form, text="Microphone").grid(row=1, column=0, sticky="w")
        self.mic_var = tk.StringVar(value=self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.mic_combo = ttk.Combobox(form, textvariable=self.mic_var, width=45, state="readonly")
        self.mic_combo.grid(row=1, column=1, sticky="we", padx=6, pady=4)

        ttk.Label(form, text="System audio (speaker)").grid(row=2, column=0, sticky="w")
        self.system_var = tk.StringVar(
            value=self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL
        )
        self.system_combo = ttk.Combobox(form, textvariable=self.system_var, width=45, state="readonly")
        self.system_combo.grid(row=2, column=1, sticky="we", padx=6, pady=4)

        ttk.Button(form, text="Refresh devices", command=self._refresh_devices).grid(
            row=1, column=2, rowspan=2, padx=(6, 0)
        )
        form.columnconfigure(1, weight=1)

        ttk.Button(self, text="Save", command=self._save).pack(anchor="w", padx=12)

        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=12, pady=(8, 0))

        note = (
            "This app doesn't call an AI API directly (governance doesn't allow that) and doesn't wait "
            "for anything back — when a meeting ends, its transcript (and any manual notes) is dropped "
            "as a text file into an \"Inbox\" folder under the folder you set here, which must be a "
            "location OneDrive/SharePoint is already syncing. Whatever picks that folder up from there "
            "(a Power Automate flow, Copilot Studio, or however it's wired up) owns managing and parsing "
            "it — this app's job stops at the handoff. Recording, transcription, and screen OCR all work "
            "with no folder configured; the meeting is just recorded and saved locally, not pushed "
            "anywhere. Projects, meetings, transcripts, and manual notes all stay in this app either "
            "way — that local record doesn't depend on the push.\n\n"
            "Microphone / system audio pick which device gets recorded — useful if you have more than "
            "one mic, or the wrong one is the Windows default. \"System default\" always follows "
            "whatever Windows currently has set as default. Watch the level meters on the Record tab "
            "to confirm a device is actually picking up audio."
        )
        ttk.Label(self, text=note, wraplength=560, justify="left", foreground="#555").pack(
            anchor="w", padx=12, pady=(12, 0)
        )

        self._refresh_status()
        self._refresh_devices()

    def _browse_sync_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a OneDrive/SharePoint-synced folder")
        if chosen:
            self.sync_dir_var.set(chosen)

    def _refresh_status(self) -> None:
        if self.app.settings.copilot_sync_dir:
            self.status_var.set(f"Copilot sync folder set: {self.app.settings.copilot_sync_dir}")
        else:
            self.status_var.set("No Copilot sync folder set — meetings are recorded but not pushed.")

    def _refresh_devices(self) -> None:
        """Repopulates the microphone/system-audio dropdowns with currently available devices."""
        from meeting_scribe.audio.device_picker import list_input_devices, list_loopback_devices

        try:
            self._input_devices = list_input_devices()
        except RuntimeError:
            self._input_devices = []  # not on Windows (e.g. dev machine)
        try:
            self._loopback_devices = list_loopback_devices()
        except RuntimeError:
            self._loopback_devices = []

        mic_values = [SYSTEM_DEFAULT_LABEL] + [d.name for d in self._input_devices]
        system_values = [SYSTEM_DEFAULT_LABEL] + [d.name for d in self._loopback_devices]
        self.mic_combo["values"] = mic_values
        self.system_combo["values"] = system_values
        if self.mic_var.get() not in mic_values:
            self.mic_var.set(SYSTEM_DEFAULT_LABEL)
        if self.system_var.get() not in system_values:
            self.system_var.set(SYSTEM_DEFAULT_LABEL)

    def _save(self) -> None:
        mic_name = None if self.mic_var.get() == SYSTEM_DEFAULT_LABEL else self.mic_var.get()
        system_name = (
            None if self.system_var.get() == SYSTEM_DEFAULT_LABEL else self.system_var.get()
        )

        self.app.settings = update_copilot_settings(
            self.app.settings, copilot_sync_dir=self.sync_dir_var.get().strip()
        )
        self.app.settings = update_audio_devices(
            self.app.settings, mic_device_name=mic_name, system_device_name=system_name
        )
        self._refresh_status()
        self.app.sync_device_displays()
        messagebox.showinfo("Meeting Scribe", "Settings saved.")

    def sync_from_settings(self) -> None:
        """Reflects a device change made elsewhere (e.g. the Record tab's own dropdowns) so this tab's
        dropdowns don't show a stale value for the same setting."""
        self.mic_var.set(self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.system_var.set(self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL)
