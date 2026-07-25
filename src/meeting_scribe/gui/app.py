"""Tkinter control panel: start/stop meetings, browse past meetings per project, upload context
documents, and ask questions about a project.

Windows desktop only (ships with Python's standard install there). Recording and note generation run on
a background thread so the UI doesn't freeze during transcription or the Claude call.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from meeting_scribe.ai.search import ask as ask_project
from meeting_scribe.config import Settings, load_settings
from meeting_scribe.session import MeetingSession
from meeting_scribe.storage.database import Database
from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text


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

    def _on_close(self) -> None:
        self.db.close()
        self.destroy()


class RecordTab(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app

        form = ttk.Frame(self)
        form.pack(fill="x", padx=12, pady=12)

        ttk.Label(form, text="Project").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(form, textvariable=self.project_var, width=40)
        self.project_combo.grid(row=0, column=1, sticky="we", padx=6, pady=4)

        ttk.Label(form, text="Meeting title").grid(row=1, column=0, sticky="w")
        self.title_var = tk.StringVar(value="Untitled meeting")
        ttk.Entry(form, textvariable=self.title_var, width=42).grid(
            row=1, column=1, sticky="we", padx=6, pady=4
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

        self.output = tk.Text(self, wrap="word")
        self.output.pack(fill="both", expand=True, padx=12, pady=12)

        self.refresh_projects()

    def refresh_projects(self) -> None:
        names = [p.name for p in self.app.db.list_projects()]
        self.project_combo["values"] = names

    def _start(self) -> None:
        project_name = self.project_var.get().strip()
        if not project_name:
            messagebox.showerror("Meeting Scribe", "Enter a project name first.")
            return
        try:
            session = MeetingSession(self.app.settings, self.app.db, project_name, self.title_var.get())
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

        ttk.Label(left, text="Projects").pack(anchor="w")
        self.project_list = tk.Listbox(left, width=28, height=10, exportselection=False)
        self.project_list.pack()
        self.project_list.bind("<<ListboxSelect>>", self._on_project_selected)

        ttk.Label(left, text="Meetings").pack(anchor="w", pady=(12, 0))
        self.meeting_list = tk.Listbox(left, width=28, height=10, exportselection=False)
        self.meeting_list.pack()
        self.meeting_list.bind("<<ListboxSelect>>", self._on_meeting_selected)

        ttk.Button(left, text="Upload Document…", command=self._upload_document).pack(
            fill="x", pady=(12, 0)
        )

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=12, pady=12)

        self.viewer = tk.Text(right, wrap="word", height=18)
        self.viewer.pack(fill="both", expand=True)

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

    def _on_project_selected(self, _event) -> None:
        project = self._selected_project()
        self.meeting_list.delete(0, "end")
        self._meetings = []
        if project is None:
            return
        self._meetings = self.app.db.list_meetings(project.id)
        for meeting in self._meetings:
            self.meeting_list.insert("end", f"{meeting.started_at}  {meeting.title}")

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
