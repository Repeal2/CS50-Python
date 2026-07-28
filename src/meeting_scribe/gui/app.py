"""Tkinter control panel: start/stop meetings, browse past meetings per project, upload context
documents, and ask questions about a project.

Windows desktop only (ships with Python's standard install there). Recording and note generation run on
a background thread so the UI doesn't freeze during transcription or the (now much slower, since it
round-trips through a Power Automate/Copilot Studio flow) notes-generation wait.
"""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from meeting_scribe.ai.copilot_bridge import CopilotResponseTimeout
from meeting_scribe.ai.search import ask as ask_project
from meeting_scribe.config import (
    DEFAULT_NOTES_SYSTEM_PROMPT,
    Settings,
    load_settings,
    update_audio_devices,
    update_copilot_settings,
    update_notes_system_prompt,
)
from meeting_scribe.screen.region_picker import RegionOutline, RegionTarget, pick_region_interactively
from meeting_scribe.session import MeetingSession
from meeting_scribe.storage.database import Database
from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text
from meeting_scribe.transcription.engine import TranscriptLine, render_transcript

# Shown in a device dropdown in place of a device name, meaning "follow whatever Windows currently
# considers the default" rather than a specific device — used on both the Record tab and Settings tab.
SYSTEM_DEFAULT_LABEL = "System default"


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


class MeetingScribeApp(tk.Tk):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.title("Meeting Scribe")
        self.geometry("980x680")

        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        self._session: MeetingSession | None = None

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


class RecordTab(ttk.Frame):
    ENTIRE_SCREEN_LABEL = "Entire screen"

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._window_targets: list = []
        self._region_target: RegionTarget | None = None
        self._region_outline: RegionOutline | None = None
        self._input_devices: list = []
        self._loopback_devices: list = []

        form = ttk.Frame(self)
        form.pack(fill="x", padx=12, pady=12)

        ttk.Label(form, text="Project").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(form, textvariable=self.project_var, width=40)
        self.project_combo.grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Button(form, text="Add Project", command=self.app.prompt_new_project).grid(
            row=0, column=2, padx=(6, 0)
        )

        ttk.Label(form, text="Meeting title").grid(row=1, column=0, sticky="w")
        self.title_var = tk.StringVar(value="Untitled meeting")
        ttk.Entry(form, textvariable=self.title_var, width=42).grid(
            row=1, column=1, sticky="we", padx=6, pady=4
        )

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

        ttk.Label(form, text="Microphone").grid(row=3, column=0, sticky="w")
        self.mic_var = tk.StringVar(value=self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.mic_combo = ttk.Combobox(form, textvariable=self.mic_var, width=40, state="readonly")
        self.mic_combo.grid(row=3, column=1, sticky="we", padx=6, pady=4)
        self.mic_combo.bind("<<ComboboxSelected>>", self._on_devices_changed)

        ttk.Label(form, text="System audio (speaker)").grid(row=4, column=0, sticky="w")
        self.system_var = tk.StringVar(
            value=self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL
        )
        self.system_combo = ttk.Combobox(form, textvariable=self.system_var, width=40, state="readonly")
        self.system_combo.grid(row=4, column=1, sticky="we", padx=6, pady=4)
        self.system_combo.bind("<<ComboboxSelected>>", self._on_devices_changed)

        ttk.Button(form, text="Refresh devices", command=self._refresh_devices).grid(
            row=3, column=2, rowspan=2, padx=(6, 0)
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

        manual_notes_frame = ttk.Frame(self)
        manual_notes_frame.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(manual_notes_frame, text="Manual notes").pack(side="left")
        self.manual_note_var = tk.StringVar()
        self.manual_note_entry = ttk.Entry(
            manual_notes_frame, textvariable=self.manual_note_var, state="disabled"
        )
        self.manual_note_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.manual_note_entry.bind("<Return>", self._add_manual_note)
        self.add_note_button = ttk.Button(
            manual_notes_frame, text="Add Note", command=self._add_manual_note, state="disabled"
        )
        self.add_note_button.pack(side="left")

        # A running activity log, not a transcript preview — the raw transcript/OCR text is already
        # long-form and belongs on the Projects & Search tab once a meeting is saved. This is just enough
        # to confirm at a glance that something is actually happening during the (now potentially
        # multi-minute) wait for Copilot Studio.
        ttk.Label(self, text="Activity log").pack(anchor="w", padx=12, pady=(8, 0))
        output_frame = ttk.Frame(self)
        output_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.output = tk.Text(output_frame, wrap="word", state="disabled")
        output_scrollbar = ttk.Scrollbar(output_frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scrollbar.set)
        self.output.pack(side="left", fill="both", expand=True)
        output_scrollbar.pack(side="right", fill="y")

        self._manual_notes_lines: list[str] = []

        self.refresh_projects()
        self._refresh_windows()
        self._refresh_devices()
        self._poll_audio_levels()

    def refresh_projects(self) -> None:
        names = [p.name for p in self.app.db.list_projects()]
        self.project_combo["values"] = names

    def _poll_audio_levels(self) -> None:
        """Runs continuously (not just while recording) so the meters are always current — cheap enough
        (two float reads, ~7x/sec) that there's no need to start/stop it."""
        session = self.app._session
        mic_level, system_level = session.audio_levels() if session is not None else (0.0, 0.0)
        self.mic_level_bar["value"] = mic_level * 100
        self.system_level_bar["value"] = system_level * 100
        self.after(150, self._poll_audio_levels)

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
        whatever custom area is currently picked (if any) as an option alongside them."""
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
        self._sync_region_outline()

    def _pick_region(self) -> None:
        """Opens the drag-to-select overlay, and if the user completes a selection, adds it to the
        screen-source dropdown as a new option and switches to it immediately."""
        region = pick_region_interactively(self)
        if region is None:
            return
        self._region_target = region
        self._refresh_windows()
        self.source_var.set(region.label)
        self._sync_region_outline()

    def _sync_region_outline(self) -> None:
        """Shows a live boundary around the custom area on screen for as long as it's the selected
        screen source — not just at the moment it was drawn — so it's always obvious what's being
        captured, the same idea as the audio level meters. Hides it the moment something else is
        picked instead."""
        if self._region_outline is not None:
            self._region_outline.close()
            self._region_outline = None
        if self._region_target is not None and self.source_var.get() == self._region_target.label:
            self._region_outline = RegionOutline(self, self._region_target)

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
        self._manual_notes_lines = []
        self.status_var.set(f"Recording — {project_name} / {meeting_title}")
        self.start_button["state"] = "disabled"
        self.stop_button["state"] = "normal"
        self.manual_note_entry["state"] = "normal"
        self.add_note_button["state"] = "normal"

        self.output["state"] = "normal"
        self.output.delete("1.0", "end")
        self.output["state"] = "disabled"
        self._log(f"Recording started — Project: {project_name} / Meeting: {meeting_title}")
        self._sync_region_outline()

    def _add_manual_note(self, _event=None) -> None:
        text = self.manual_note_var.get().strip()
        session = self.app._session
        if not text or session is None:
            return
        timestamp = datetime.now().strftime("%b %d, %Y %I:%M:%S %p")
        self._manual_notes_lines.append(f"[{timestamp}] {text}")
        self.app.db.set_manual_notes(session.meeting_id, "\n".join(self._manual_notes_lines))
        self.manual_note_var.set("")
        self._log(f"Note added: {text}")

    def _stop(self) -> None:
        session = self.app._session
        if session is None:
            return
        self.stop_button["state"] = "disabled"
        self.manual_note_entry["state"] = "disabled"
        self.add_note_button["state"] = "disabled"
        self.status_var.set(
            "Transcribing, then waiting on Copilot Studio for notes… this can take a few minutes."
        )
        self._log("Stop requested by user.")
        # The custom-area boundary is a "this is what's being captured" indicator — leaving it on screen
        # after recording stops is just a stray colored box with nothing behind it. _start() re-shows it
        # if the same area is still selected next time.
        self.close_region_outline()

        def worker() -> None:
            try:
                session.stop(on_progress=lambda message: self.after(0, self._log, message))
            except Exception as exc:  # surfaced to the user regardless of cause
                self.after(0, self._on_stop_failed, exc)
                return
            self.after(0, self._on_stop_done, session.notes_timed_out)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_done(self, notes_timed_out: bool) -> None:
        if notes_timed_out:
            self.status_var.set("Saved — but notes generation timed out; the plain transcript was saved.")
        else:
            self.status_var.set("Saved.")
        self.start_button["state"] = "normal"
        self.app._session = None
        self.app.refresh_project_lists()

    def _on_stop_failed(self, exc: Exception) -> None:
        self.status_var.set("Failed.")
        self.start_button["state"] = "normal"
        self.app._session = None
        self._log(f"Failed: {exc}")
        messagebox.showerror("Meeting Scribe", f"Couldn't finish the meeting: {exc}")

    def _upload_document(self) -> None:
        """Attaches a document to the current project — and, if a meeting is actively recording (or
        still finishing up), to that specific meeting — without needing to switch to the Projects &
        Search tab and pick things from a list."""
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

        project = self.app.db.get_or_create_project(project_name)
        session = self.app._session
        meeting_id = session.meeting_id if session is not None else None
        self.app.db.add_document(project.id, path.name, text, meeting_id=meeting_id)
        self.app.refresh_project_lists()

        if meeting_id is not None:
            self._log(f"Document attached: {path.name}")
            messagebox.showinfo("Meeting Scribe", f'Added {path.name} to this meeting.')
        else:
            messagebox.showinfo("Meeting Scribe", f'Added {path.name} to project "{project.name}".')


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
        self._build_documents_tab()

        ask_row = ttk.Frame(right)
        ask_row.pack(fill="x", pady=(8, 0))
        self.question_var = tk.StringVar()
        ttk.Entry(ask_row, textvariable=self.question_var).pack(side="left", fill="x", expand=True)
        ttk.Button(ask_row, text="Ask", command=self._ask).pack(side="left", padx=(6, 0))

        answer_frame = ttk.Frame(right)
        answer_frame.pack(fill="both", pady=(6, 0))
        self.answer_text = tk.Text(answer_frame, wrap="word", height=6)
        answer_scrollbar = ttk.Scrollbar(
            answer_frame, orient="vertical", command=self.answer_text.yview
        )
        self.answer_text.configure(yscrollcommand=answer_scrollbar.set)
        self.answer_text.pack(side="left", fill="both", expand=True)
        answer_scrollbar.pack(side="right", fill="y")

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
        for widget in (self.ocr_text, self.audio_text, self.manual_notes_text):
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

    def _ask(self) -> None:
        project = self._selected_project()
        question = self.question_var.get().strip()
        if project is None or not question:
            return
        settings = self.app.settings
        if settings.copilot_sync_dir is None:
            messagebox.showerror(
                "Meeting Scribe", "Set up the Copilot sync folder in the Settings tab to ask questions."
            )
            return
        _set_text(self.answer_text, "Waiting on Copilot Studio… this can take a while.")

        def worker() -> None:
            try:
                answer = ask_project(
                    self.app.db,
                    project.id,
                    question,
                    inbox_dir=settings.copilot_inbox_dir,
                    outbox_dir=settings.copilot_outbox_dir,
                    poll_interval_seconds=settings.copilot_poll_interval_seconds,
                    timeout_seconds=settings.copilot_timeout_seconds,
                )
            except CopilotResponseTimeout as exc:
                answer = str(exc)
            except Exception as exc:  # surfaced to the user regardless of cause
                answer = f"Couldn't answer that: {exc}"
            self.after(0, self._show_answer, answer)

        threading.Thread(target=worker, daemon=True).start()

    def _show_answer(self, answer: str) -> None:
        _set_text(self.answer_text, answer)


class SettingsTab(ttk.Frame):
    """Copilot Studio bridge folder, notes prompt, and audio device selection — all editable without
    touching environment variables. Saved settings are written to disk (see config.save_user_config)
    and take effect immediately for this running session."""

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._input_devices: list = []
        self._loopback_devices: list = []

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

        ttk.Label(form, text="Poll interval (seconds)").grid(row=1, column=0, sticky="w")
        self.poll_interval_var = tk.StringVar(value=str(self.app.settings.copilot_poll_interval_seconds))
        ttk.Entry(form, textvariable=self.poll_interval_var, width=10).grid(
            row=1, column=1, sticky="w", padx=6, pady=4
        )

        ttk.Label(form, text="Timeout (seconds)").grid(row=2, column=0, sticky="w")
        self.timeout_var = tk.StringVar(value=str(self.app.settings.copilot_timeout_seconds))
        ttk.Entry(form, textvariable=self.timeout_var, width=10).grid(
            row=2, column=1, sticky="w", padx=6, pady=4
        )

        ttk.Label(form, text="Microphone").grid(row=3, column=0, sticky="w")
        self.mic_var = tk.StringVar(value=self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.mic_combo = ttk.Combobox(form, textvariable=self.mic_var, width=45, state="readonly")
        self.mic_combo.grid(row=3, column=1, sticky="we", padx=6, pady=4)

        ttk.Label(form, text="System audio (speaker)").grid(row=4, column=0, sticky="w")
        self.system_var = tk.StringVar(
            value=self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL
        )
        self.system_combo = ttk.Combobox(form, textvariable=self.system_var, width=45, state="readonly")
        self.system_combo.grid(row=4, column=1, sticky="we", padx=6, pady=4)

        ttk.Button(form, text="Refresh devices", command=self._refresh_devices).grid(
            row=3, column=2, rowspan=2, padx=(6, 0)
        )
        form.columnconfigure(1, weight=1)

        prompt_header = ttk.Frame(self)
        prompt_header.pack(fill="x", padx=12, pady=(4, 0))
        ttk.Label(prompt_header, text="Notes system prompt").pack(side="left")
        ttk.Button(
            prompt_header, text="Reset to recommended default", command=self._reset_system_prompt
        ).pack(side="right")

        prompt_frame = ttk.Frame(self)
        prompt_frame.pack(fill="both", expand=True, padx=12, pady=(4, 4))
        self.prompt_text = tk.Text(prompt_frame, wrap="word", height=12)
        prompt_scrollbar = ttk.Scrollbar(prompt_frame, orient="vertical", command=self.prompt_text.yview)
        self.prompt_text.configure(yscrollcommand=prompt_scrollbar.set)
        self.prompt_text.insert("1.0", self.app.settings.notes_system_prompt)
        self.prompt_text.pack(side="left", fill="both", expand=True)
        prompt_scrollbar.pack(side="right", fill="y")

        ttk.Button(self, text="Save", command=self._save).pack(anchor="w", padx=12)

        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=12, pady=(8, 0))

        note = (
            "Meeting notes and Ask no longer call an AI API directly (governance doesn't allow that) — "
            "instead, the transcript is dropped as a text file into an \"Inbox\" folder under the folder "
            "you set here, which must be a location OneDrive/SharePoint is already syncing. A Power "
            "Automate flow (built and owned outside this app, in the Power Platform admin UI) is "
            "expected to pick it up, run it through Copilot Studio, and write the result back into a "
            "matching \"Outbox\" folder. This app just waits, polling every \"poll interval\" up to "
            "\"timeout\" — recording, transcription, and screen OCR all work with no folder configured.\n\n"
            "Microphone / system audio pick which device gets recorded — useful if you have more than "
            "one mic, or the wrong one is the Windows default. \"System default\" always follows "
            "whatever Windows currently has set as default. Watch the level meters on the Record tab "
            "to confirm a device is actually picking up audio.\n\n"
            "The notes system prompt is bundled into the request file sent to the flow — edit it to "
            "change the tone, sections, or level of detail of the generated notes (whoever builds the "
            "flow needs to pass the file's content straight through to the GPT/Copilot Studio action)."
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

    def _reset_system_prompt(self) -> None:
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", DEFAULT_NOTES_SYSTEM_PROMPT)

    def _refresh_status(self) -> None:
        if self.app.settings.copilot_sync_dir:
            self.status_var.set(f"Copilot sync folder set: {self.app.settings.copilot_sync_dir}")
        else:
            self.status_var.set(
                "No Copilot sync folder set — notes generation and Ask are disabled until one is."
            )

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
        try:
            poll_interval_seconds = float(self.poll_interval_var.get())
            timeout_seconds = float(self.timeout_var.get())
        except ValueError:
            messagebox.showerror("Meeting Scribe", "Poll interval and timeout must be numbers.")
            return

        self.app.settings = update_copilot_settings(
            self.app.settings,
            copilot_sync_dir=self.sync_dir_var.get().strip(),
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        self.app.settings = update_audio_devices(
            self.app.settings, mic_device_name=mic_name, system_device_name=system_name
        )
        self.app.settings = update_notes_system_prompt(
            self.app.settings, notes_system_prompt=self.prompt_text.get("1.0", "end")
        )
        self._refresh_status()
        self.app.sync_device_displays()
        messagebox.showinfo("Meeting Scribe", "Settings saved.")

    def sync_from_settings(self) -> None:
        """Reflects a device change made elsewhere (e.g. the Record tab's own dropdowns) so this tab's
        dropdowns don't show a stale value for the same setting."""
        self.mic_var.set(self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.system_var.set(self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL)
