"""Tkinter control panel: start/stop meetings, browse past meetings per project, upload context
documents, and ask questions about a project.

Windows desktop only (ships with Python's standard install there). Recording and note generation run on
a background thread so the UI doesn't freeze during transcription or the Claude call.
"""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from meeting_scribe.ai.search import ask as ask_project
from meeting_scribe.config import Settings, load_settings
from meeting_scribe.session import MeetingSession
from meeting_scribe.storage.database import Database
from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text


def _format_meeting_timestamp(iso_string: str) -> str:
    """Renders a stored UTC ISO timestamp (e.g. "2026-07-25T12:17:32.123456+00:00") in the user's local
    time and a human-readable format, instead of the raw string."""
    try:
        return datetime.fromisoformat(iso_string).astimezone().strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return iso_string


class MeetingScribeApp(tk.Tk):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.title("Meeting Scribe")
        self.geometry("760x560")

        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        self._session: MeetingSession | None = None

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self._record_tab = RecordTab(notebook, self)
        self._projects_tab = ProjectsTab(notebook, self)
        notebook.add(self._record_tab, text="Record Meeting")
        notebook.add(self._projects_tab, text="Projects & Search")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def refresh_project_lists(self) -> None:
        self._record_tab.refresh_projects()
        self._projects_tab.refresh_projects()

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
        self.db.close()
        self.destroy()


class RecordTab(ttk.Frame):
    ENTIRE_SCREEN_LABEL = "Entire screen"

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._window_targets: list = []

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
        ttk.Button(form, text="Refresh windows", command=self._refresh_windows).grid(
            row=2, column=2, padx=(6, 0)
        )
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12)
        self.start_button = ttk.Button(buttons, text="Start Meeting", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop Meeting", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=12, pady=(8, 0))

        output_frame = ttk.Frame(self)
        output_frame.pack(fill="both", expand=True, padx=12, pady=12)
        self.output = tk.Text(output_frame, wrap="word")
        output_scrollbar = ttk.Scrollbar(output_frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scrollbar.set)
        self.output.pack(side="left", fill="both", expand=True)
        output_scrollbar.pack(side="right", fill="y")

        self.refresh_projects()
        self._refresh_windows()

    def refresh_projects(self) -> None:
        names = [p.name for p in self.app.db.list_projects()]
        self.project_combo["values"] = names

    def select_project(self, name: str) -> None:
        self.project_var.set(name)

    def _refresh_windows(self) -> None:
        """Repopulates the screen-source dropdown with currently open, titled windows the user can
        pick as an OCR target instead of the whole screen (e.g. just the Teams/Zoom window)."""
        from meeting_scribe.screen.window_picker import list_capturable_windows

        try:
            self._window_targets = list_capturable_windows()
        except RuntimeError:
            self._window_targets = []  # not on Windows (e.g. dev machine) — whole-screen only
        values = [self.ENTIRE_SCREEN_LABEL] + [w.title for w in self._window_targets]
        self.source_combo["values"] = values
        if self.source_var.get() not in values:
            self.source_var.set(self.ENTIRE_SCREEN_LABEL)

    def _selected_screen_target(self):
        selected = self.source_var.get()
        if selected == self.ENTIRE_SCREEN_LABEL:
            return None
        return next((w for w in self._window_targets if w.title == selected), None)

    def _start(self) -> None:
        project_name = self.project_var.get().strip()
        if not project_name:
            messagebox.showerror("Meeting Scribe", "Enter a project name first.")
            return
        try:
            session = MeetingSession(
                self.app.settings,
                self.app.db,
                project_name,
                self.title_var.get(),
                screen_target=self._selected_screen_target(),
            )
            session.start()
        except RuntimeError as exc:
            messagebox.showerror("Meeting Scribe", str(exc))
            return

        self.app._session = session
        self.status_var.set(f"Recording — {project_name} / {self.title_var.get()}")
        self.start_button["state"] = "disabled"
        self.stop_button["state"] = "normal"
        self.output.delete("1.0", "end")

    def _stop(self) -> None:
        session = self.app._session
        if session is None:
            return
        self.stop_button["state"] = "disabled"
        self.status_var.set("Transcribing and generating notes… this can take a minute.")

        def worker() -> None:
            try:
                result = session.stop()
            except Exception as exc:  # surfaced to the user regardless of cause
                self.after(0, self._on_stop_failed, exc)
                return
            self.after(0, self._on_stop_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_done(self, result: str) -> None:
        self.output.delete("1.0", "end")
        self.output.insert("1.0", result)
        self.status_var.set("Saved.")
        self.start_button["state"] = "normal"
        self.app._session = None
        self.app.refresh_project_lists()

    def _on_stop_failed(self, exc: Exception) -> None:
        self.status_var.set("Failed.")
        self.start_button["state"] = "normal"
        self.app._session = None
        messagebox.showerror("Meeting Scribe", f"Couldn't finish the meeting: {exc}")


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

        ttk.Button(left, text="Upload Document…", command=self._upload_document).pack(
            fill="x", pady=(12, 0)
        )

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=12, pady=12)

        viewer_frame = ttk.Frame(right)
        viewer_frame.pack(fill="both", expand=True)
        self.viewer = tk.Text(viewer_frame, wrap="word", height=18)
        viewer_scrollbar = ttk.Scrollbar(viewer_frame, orient="vertical", command=self.viewer.yview)
        self.viewer.configure(yscrollcommand=viewer_scrollbar.set)
        self.viewer.pack(side="left", fill="both", expand=True)
        viewer_scrollbar.pack(side="right", fill="y")

        ask_row = ttk.Frame(right)
        ask_row.pack(fill="x", pady=(8, 0))
        self.question_var = tk.StringVar()
        ttk.Entry(ask_row, textvariable=self.question_var).pack(side="left", fill="x", expand=True)
        ttk.Button(ask_row, text="Ask", command=self._ask).pack(side="left", padx=(6, 0))

        self._projects: list = []
        self._meetings: list = []
        self.refresh_projects()

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
        if project is None:
            return
        self._meetings = self.app.db.list_meetings(project.id)
        for meeting in self._meetings:
            timestamp = _format_meeting_timestamp(meeting.started_at)
            self.meeting_list.insert("end", f"{meeting.title} — {timestamp}")

    def _on_meeting_selected(self, _event) -> None:
        selection = self.meeting_list.curselection()
        if not selection:
            return
        meeting = self._meetings[selection[0]]
        self.viewer.delete("1.0", "end")
        content = meeting.notes_markdown or meeting.transcript_text or "(no transcript yet)"
        self.viewer.insert("1.0", content)

    def _upload_document(self) -> None:
        project = self._selected_project()
        if project is None:
            messagebox.showerror("Meeting Scribe", "Select a project first.")
            return
        path_str = filedialog.askopenfilename(
            title="Attach a document to this project",
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
        self.app.db.add_document(project.id, path.name, text)
        messagebox.showinfo("Meeting Scribe", f"Added {path.name} to {project.name}.")

    def _ask(self) -> None:
        project = self._selected_project()
        question = self.question_var.get().strip()
        if project is None or not question:
            return
        if not self.app.settings.anthropic_api_key:
            messagebox.showerror("Meeting Scribe", "Set ANTHROPIC_API_KEY to ask questions.")
            return
        self.viewer.delete("1.0", "end")
        self.viewer.insert("1.0", "Thinking…")

        def worker() -> None:
            try:
                answer = ask_project(
                    self.app.db,
                    project.id,
                    question,
                    api_key=self.app.settings.anthropic_api_key,
                    model=self.app.settings.anthropic_model,
                )
            except Exception as exc:  # surfaced to the user regardless of cause
                answer = f"Couldn't answer that: {exc}"
            self.after(0, self._show_answer, answer)

        threading.Thread(target=worker, daemon=True).start()

    def _show_answer(self, answer: str) -> None:
        self.viewer.delete("1.0", "end")
        self.viewer.insert("1.0", answer)
