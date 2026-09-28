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
from tkinter import font as tkfont
from typing import Callable

from meeting_scribe import __version__, action_tracker
from meeting_scribe.ai.minutes_import import MinutesImporter
from meeting_scribe.audio.device_watch import device_signature
from meeting_scribe.config import (
    DEFAULT_TRANSCRIPTION_LANGUAGE,
    TRANSCRIPTION_LANGUAGES,
    WHISPER_MODEL_SIZES,
    Settings,
    default_data_dir,
    load_settings,
    update_settings,
)
from meeting_scribe.gui.meeting_prompt import PromptCard, start_recording_prompt, stop_recording_prompt
from meeting_scribe.gui.theme import Palette, apply_theme, badge_colours, style_text
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
from meeting_scribe.transcription import jobs
from meeting_scribe.transcription.engine import render_transcript
from meeting_scribe.transcription.jobs import JobBoard, JobSnapshot

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

# Notes formatting is kept as Markdown-style markers in the saved text — "# " / "## " headings, **bold**,
# _italic_ — so it stays readable when exported or pushed as plain text, and the editor styles it live.
_NOTES_HEADING = re.compile(r"^(#{1,2}) \S.*$")
_NOTES_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_NOTES_ITALIC = re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)")
_NOTES_LIST_MARKER = re.compile(r"^([ \t]*)([-*•] )?")
_NOTES_HEADING_MARKER = re.compile(r"^#{1,2} ")

_NO_PREVIOUS_MINUTES = (
    "When this is a recurring meeting — the same title in the same project — the last meeting's minutes "
    "show here."
)
_NO_MINUTES = (
    "No minutes yet. They appear here once Copilot Studio has written them to the sync folder "
    "(Settings) — the app checks every minute."
)

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
        ("Meeting minutes", meeting.minutes),
        ("Attendees", meeting.attendees),
    ):
        if body and body.strip():
            sections.append(f"{heading}\n{'-' * len(heading)}\n{body.strip()}")
    return "\n\n".join(sections) + "\n"


def _format_took(seconds: float) -> str:
    """How long a transcription took or has been going: "45 s", "12 min", "1 h 05 min"."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _format_eta(seconds: float | None) -> str:
    """A running job's estimate: "about 4 min left" — "" while there's nothing to go on."""
    if seconds is None:
        return ""
    if seconds < 60:
        return "less than a minute left"
    return f"about {_format_took(seconds)} left"


def _job_progress_text(job: JobSnapshot) -> str:
    """The line under a running job's name: its stage, how far along, and how long to go."""
    parts = [job.stage]
    if job.fraction is not None and job.stage == jobs.LOCAL:
        parts.append(f"{job.fraction:.0%}")
    parts.append(f"{_format_took(job.elapsed_seconds)} so far")
    eta = _format_eta(job.eta_seconds)
    if eta:
        parts.append(eta)
    return "  ·  ".join(parts)


_RUN_OUTCOMES = {jobs.RUNNING: "Running", jobs.DONE: "Done", jobs.FAILED: "Failed"}


def _run_outcome(row, job: JobSnapshot | None) -> tuple[str, str]:
    """(result in words, error if any) for a transcription run. `job` is this process's job for it, if
    there is one — fresher than the database row. A run still marked running that no job here owns was
    cut short by the app closing or crashing."""
    if job is not None:
        return _RUN_OUTCOMES[job.state], job.error or ""
    if row["outcome"] == jobs.RUNNING:
        return "Interrupted", "the app was closed or crashed before it finished"
    return _RUN_OUTCOMES.get(row["outcome"], row["outcome"]), row["detail"] or ""


def _format_run_time(iso_string: str) -> str:
    """"Sep 25 3:45 PM" — when a transcription run started, short enough for the log's When column."""
    try:
        return datetime.fromisoformat(iso_string).astimezone().strftime("%b %d %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso_string


def _run_took(row) -> str:
    if not row["finished_at"]:
        return ""
    try:
        seconds = (datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
    except ValueError:
        return ""
    return _format_took(seconds)


def _meeting_transcription_status(meeting: Meeting, engines: "set[str]", in_progress: bool) -> str:
    """A meeting's line in the Transcriptions log: which transcripts it has, or why it has none."""
    if in_progress:
        return "In progress"
    if meeting.ended_at is None:
        return "Not transcribed"
    names = [name for engine, name in ((LOCAL_ENGINE, "This PC"), (CLOUD_ENGINE, "Cloud")) if engine in engines]
    return " + ".join(names) if names else "No speech found"


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


def _language_label(code: str | None) -> str:
    return dict(TRANSCRIPTION_LANGUAGES).get(code, dict(TRANSCRIPTION_LANGUAGES)[DEFAULT_TRANSCRIPTION_LANGUAGE])


def _language_code(label: str) -> str | None:
    return next((code for code, name in TRANSCRIPTION_LANGUAGES if name == label), DEFAULT_TRANSCRIPTION_LANGUAGE)


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


def _card(parent: tk.Misc, title: str | None = None, *, padding: int | tuple[int, ...] = 16) -> tuple[ttk.Frame, ttk.Frame]:
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


def _notes_format_spans(line: str) -> list[tuple[str, int, int]]:
    """(tag, start column, end column) spans styling one line of notes: the heading, bold and italic text,
    and the markers around them, which are dimmed rather than hidden so they stay editable."""
    spans: list[tuple[str, int, int]] = []
    heading = _NOTES_HEADING.match(line)
    if heading:
        marker_end = len(heading.group(1)) + 1
        spans.append(("h1" if len(heading.group(1)) == 1 else "h2", marker_end, len(line)))
        spans.append(("marker", 0, marker_end))
    for pattern, tag, marker_len in ((_NOTES_BOLD, "bold", 2), (_NOTES_ITALIC, "italic", 1)):
        for match in pattern.finditer(line):
            start, end = match.span()
            spans.append((tag, start + marker_len, end - marker_len))
            spans.append(("marker", start, start + marker_len))
            spans.append(("marker", end - marker_len, end))
    return spans


def _configure_notes_tags(widget: tk.Text, palette: Palette, fonts) -> None:
    """Sets up the tags _highlight_notes applies — for the notes editor and every read-only view of notes."""
    body = fonts.body.actual()
    widget.tag_configure("h1", font=fonts.title, spacing1=8, spacing3=2)
    widget.tag_configure("h2", font=fonts.strong, spacing1=6, spacing3=2)
    widget.tag_configure("bold", font=fonts.strong)
    widget.tag_configure("italic", font=(body["family"], body["size"], "italic"))
    widget.tag_configure("marker", foreground=palette.muted)
    widget.tag_raise("marker")


def _highlight_notes(widget: tk.Text) -> None:
    """Re-styles the notes' Markdown-style formatting — cheap enough to run after every keystroke."""
    for tag in ("h1", "h2", "bold", "italic", "marker"):
        widget.tag_remove(tag, "1.0", "end")
    for line_number, line in enumerate(widget.get("1.0", "end-1c").split("\n"), start=1):
        for tag, start, end in _notes_format_spans(line):
            widget.tag_add(tag, f"{line_number}.{start}", f"{line_number}.{end}")


def _set_readonly_notes(widget: tk.Text, content: str) -> None:
    _set_readonly_text(widget, content)
    _highlight_notes(widget)


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
    """A vertically scrolling page — Settings grows taller than a small window — or list (on a card, pass
    its `background` and `style`)."""

    def __init__(
        self, parent: tk.Misc, palette: Palette, *, padding=(28, 24), background: str | None = None,
        style: str = "TFrame",
    ):
        super().__init__(parent, style=style, borderwidth=0)
        self._canvas = tk.Canvas(
            self, background=background or palette.background, highlightthickness=0, borderwidth=0
        )
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner = ttk.Frame(self._canvas, padding=padding, style=style, borderwidth=0)
        window = self._canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self.inner.bind("<Configure>", lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(window, width=e.width))
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        # One app-wide handler that only acts over this page (<Enter>/<Leave> on the frame itself also
        # fire when the pointer crosses into its own children, so they can't gate this).
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")

    @property
    def canvas(self) -> tk.Canvas:
        return self._canvas

    def _on_wheel(self, event) -> None:
        over_page = str(event.widget).startswith(str(self)) and self.winfo_ismapped()
        if over_page and self.inner.winfo_height() > self._canvas.winfo_height():
            self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def scroll_position(self) -> float:
        return self._canvas.yview()[0]

    def restore_scroll(self, position: float) -> None:
        """Back to `position` once the content just rebuilt has been laid out."""
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.yview_moveto(position)


# --- the app --------------------------------------------------------------------------------------


class MeetingScribeApp(tk.Tk):
    _DEVICE_POLL_MS = 2000  # how often device dropdowns check for devices coming and going
    _MEETING_POLL_MS = 2000  # how often to check whether a Teams call has started
    _CLOCK_MS = 500
    _MINUTES_POLL_MS = 60_000  # how often to look for minutes Copilot Studio has written
    _NAV_LABELS = {
        "record": "●   Record",
        "library": "▤   Library",
        "actions": "☑   Actions",
        "transcriptions": "⟳   Transcriptions",
        "settings": "⚙   Settings",
    }

    def __init__(self, settings: Settings | None = None):
        _enable_per_monitor_dpi_awareness()
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1120x760")
        self.minsize(940, 640)
        self.palette, self.fonts = apply_theme(self)

        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        # Every transcription this process runs, and how far along it is — see the Transcriptions page.
        self.jobs = JobBoard(self.db)
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

        self._minutes_importer = MinutesImporter(self.db)
        self._minutes_check_running = False
        self.after(1000, self._poll_minutes)

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
        self.actions_page = ActionsPage(self._content, self)
        self.transcriptions_page = TranscriptionsPage(self._content, self)
        self.settings_page = SettingsPage(self._content, self)
        self._pages = {
            "record": self.record_page,
            "library": self.library_page,
            "actions": self.actions_page,
            "transcriptions": self.transcriptions_page,
            "settings": self.settings_page,
        }
        self._nav_buttons: dict[str, ttk.Button] = {}
        for key, label in self._NAV_LABELS.items():
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
        elif key == "actions":
            self.actions_page.on_show()
        elif key == "transcriptions":
            self.transcriptions_page.on_show()

    def open_in_library(self, meeting_id: int) -> None:
        self.show_page("library")
        self.library_page.open_meeting(meeting_id)

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
            active = self.jobs.active()
            if self._session is not None and self._session_started_at is not None:
                elapsed = _format_elapsed(time.monotonic() - self._session_started_at)
                self.title(f"● {elapsed} — {APP_NAME}")
                status = f"Recording · {elapsed}"
            else:
                elapsed = None
                self.title(APP_NAME)
                status = "Ready"
            if len(active) == 1:
                job = active[0]
                done = f" · {job.fraction:.0%}" if job.fraction is not None and job.stage == jobs.LOCAL else ""
                status += f"\nTranscribing “{job.title}”{done}"
            elif active:
                status += f"\nTranscribing {len(active)} meetings…"
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

    # -- minutes from Copilot Studio ---------------------------------------------------------------

    def _poll_minutes(self) -> None:
        self.check_for_minutes()
        self.after(self._MINUTES_POLL_MS, self._poll_minutes)

    def check_for_minutes(self, *, report: bool = False) -> None:
        """Files any new or changed minutes Copilot Studio has written to the sync folder against their
        meetings (see ai/minutes_import.py), on a background thread since the folder is on OneDrive.
        With `report`, says how it went even when nothing new turned up."""
        root = self.settings.copilot_sync_dir
        if root is None:
            if report:
                self.toast("Set a Copilot sync folder in Settings first")
            return
        if self._minutes_check_running:
            return
        self._minutes_check_running = True

        def worker() -> None:
            updated, error = [], None
            try:
                updated = self._minutes_importer.import_from(root)
            except Exception as exc:  # a folder problem mustn't stop later checks
                error = exc
            try:
                self.after(0, self._on_minutes_checked, updated, error, report)
            except (RuntimeError, tk.TclError):
                pass  # the window closed while checking

        threading.Thread(target=worker, daemon=True).start()

    def _on_minutes_checked(self, updated: list[int], error: Exception | None, report: bool) -> None:
        self._minutes_check_running = False
        if error is not None:
            if report:
                self.toast(f"Couldn't check for minutes: {error}")
            return
        if updated:
            self.library_page.on_minutes_updated(updated)
            self.record_page.refresh_previous_minutes()
            self.toast(f"Minutes added for {len(updated)} meeting{'' if len(updated) == 1 else 's'}")
        elif report:
            self.toast("No new minutes")

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
        self.transcriptions_page.refresh_history()

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
        if not self.actions_page.confirm_discard_changes():
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
        self._rec_pill = ttk.Label(actions, style="RecPill.TLabel")
        ttk.Button(actions, text="Reset OCR box", style="Link.TButton", command=self._reset_ocr_box).pack(side="right")
        ttk.Button(actions, text="Attach document", style="Link.TButton", command=self._upload_document).pack(
            side="right"
        )
        self.capture_attendees_button = ttk.Button(
            actions, text="Capture attendees", style="Link.TButton", command=self._capture_attendees,
            state="disabled",
        )
        self.capture_attendees_button.pack(side="right")
        # Shown only while recording.
        self._hint = ttk.Label(meeting, text=self._RECORDING_HINT, style="Card.Muted.TLabel")
        self._hint.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self._hint.grid_remove()

        # Audio card: device pickers with live meters right next to them.
        audio_card, audio = _card(self, padding=(14, 8))
        audio_card.pack(fill="x", pady=(12, 0))
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
        self._audio_status: str | None = None  # None forces the first _set_audio_status to hide the line
        self._set_audio_status("")

        # Notes beside the previous meeting's minutes, with the activity log along the bottom.
        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True, pady=(12, 0))
        panes = ttk.PanedWindow(body, orient="horizontal")
        body.add(panes, weight=4)

        notes_card, notes = _card(panes, padding=0)
        notes_header = ttk.Frame(notes, style="Card.TFrame", borderwidth=0, padding=(14, 8, 10, 4))
        notes_header.pack(fill="x")
        ttk.Label(notes_header, text="Notes", style="Card.Title.TLabel").pack(side="left", padx=(0, 12))
        # Formatting toolbar — each button also has a shortcut, listed in its label or below.
        self._format_buttons = []
        for text, command in (
            ("H1", lambda: self._toggle_heading(1)),
            ("H2", lambda: self._toggle_heading(2)),
            ("B", lambda: self._toggle_inline("**")),
            ("I", lambda: self._toggle_inline("_")),
            ("• List", self._toggle_bullet),
        ):
            button = ttk.Button(notes_header, text=text, style="Link.TButton", command=command, state="disabled")
            button.pack(side="left")
            self._format_buttons.append(button)
        ttk.Button(notes_header, text="Time  Ctrl+T", style="Link.TButton", command=self._insert_timestamp).pack(
            side="right"
        )
        notes_frame, self.manual_notes_editor = _scrolled_text(notes, palette, fonts.body, undo=True, height=8, width=60)
        notes_frame.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        _configure_notes_tags(self.manual_notes_editor, palette, fonts)
        self.manual_notes_editor.insert(
            "1.0",
            "Notes open here when recording starts, and save as you type.\n\n"
            "- Start a line with \"- \" and Enter continues the bullet\n"
            "    - Tab and Shift+Tab indent and outdent\n"
            "- H1 / H2 make a heading; **B** (Ctrl+B) and _I_ (Ctrl+I) bold or italicize the selection\n"
            "- Ctrl+T stamps the recording time, to match the transcript",
        )
        _highlight_notes(self.manual_notes_editor)
        self.manual_notes_editor.configure(state="disabled", foreground=palette.muted)
        self.manual_notes_editor.bind("<KeyRelease>", self._on_manual_notes_edited)
        self.manual_notes_editor.bind("<Return>", self._on_manual_notes_return)
        self.manual_notes_editor.bind("<Tab>", self._on_manual_notes_indent)
        self.manual_notes_editor.bind("<Shift-Tab>", self._on_manual_notes_dedent)
        self.manual_notes_editor.bind("<Control-t>", self._insert_timestamp)
        self.manual_notes_editor.bind("<Control-b>", lambda _e: self._toggle_inline("**"))
        self.manual_notes_editor.bind("<Control-i>", lambda _e: self._toggle_inline("_"))
        panes.add(notes_card, weight=3)

        # The last occurrence's minutes, for a recurring meeting — see refresh_previous_minutes.
        minutes_card, minutes = _card(panes, padding=0)
        minutes_header = ttk.Frame(minutes, style="Card.TFrame", borderwidth=0, padding=(14, 8, 10, 4))
        minutes_header.pack(fill="x")
        ttk.Label(minutes_header, text="Previous meeting minutes", style="Card.Title.TLabel").pack(side="left")
        self._previous_minutes_when = ttk.Label(minutes_header, style="Card.Muted.TLabel")
        self._previous_minutes_when.pack(side="right")
        minutes_frame, self.previous_minutes = _scrolled_text(
            minutes, palette, fonts.body, state="disabled", height=8, width=40
        )
        minutes_frame.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        _configure_notes_tags(self.previous_minutes, palette, fonts)
        panes.add(minutes_card, weight=2)

        log_card, log = _card(body, padding=0)
        log_header = ttk.Frame(log, style="Card.TFrame", borderwidth=0, padding=(14, 6, 10, 2))
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Activity", style="Card.Title.TLabel").pack(side="left")
        ttk.Button(log_header, text="Clear", style="Link.TButton", command=self._clear_log).pack(side="right")
        log_frame, self.output = _scrolled_text(log, palette, fonts.mono, state="disabled", height=4)
        self.output.configure(foreground=palette.muted)
        log_frame.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        body.add(log_card, weight=1)

        self.refresh_projects()
        self.refresh_previous_minutes()
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
            self._rec_pill.pack(side="left", padx=(12, 0))

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
        self.refresh_previous_minutes()
        session = self.app._session
        project_name = self.project_var.get().strip()
        if session is None or not project_name or project_name == session.project.name:
            return
        session.set_project(project_name)
        self.log(f'[{session.title}] Moved to project "{project_name}".')

    def _on_title_changed(self, *_args) -> None:
        """The title stays editable for the whole meeting; each change is saved straight away."""
        session = self.app._session
        if session is not None:
            session.title = self.title_var.get()
            self.app.db.update_meeting_title(session.meeting_id, session.title)
        self.refresh_previous_minutes()

    def refresh_previous_minutes(self) -> None:
        """Shows the minutes of the last meeting with this project and title — a recurring meeting's
        previous occurrence — so they can be read before and during the next one."""
        session = self.app._session
        project = self.app.db.get_project_by_name(self.project_var.get().strip())
        title = self.title_var.get().strip()
        previous = None
        if project is not None and title and title != DEFAULT_MEETING_TITLE:
            previous = self.app.db.get_previous_occurrence(
                project.id, title, exclude_meeting_id=session.meeting_id if session is not None else None
            )
        if previous is None:
            self._previous_minutes_when.configure(text="")
            _set_readonly_text(self.previous_minutes, _NO_PREVIOUS_MINUTES)
            self.previous_minutes.configure(foreground=self.app.palette.muted)
            return
        self._previous_minutes_when.configure(text=_format_short_date(previous.started_at))
        if previous.minutes:
            _set_readonly_notes(self.previous_minutes, previous.minutes)
            self.previous_minutes.configure(foreground=self.app.palette.text)
        else:
            _set_readonly_text(self.previous_minutes, _NO_MINUTES)
            self.previous_minutes.configure(foreground=self.app.palette.muted)

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
        self._hint.grid()

        self.manual_notes_editor.configure(state="normal", foreground=self.app.palette.text)
        self.manual_notes_editor.delete("1.0", "end")
        self.manual_notes_editor.edit_reset()
        _highlight_notes(self.manual_notes_editor)
        self.manual_notes_editor.focus_set()
        for button in self._format_buttons:
            button.configure(state="normal")
        self.refresh_previous_minutes()

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
        self._hint.grid_remove()
        self.manual_notes_editor.configure(state="disabled")
        for button in self._format_buttons:
            button.configure(state="disabled")
        self.log(f"[{session.title}] Stopped — transcribing in the background (progress under Transcriptions).")
        self.close_ocr_box()
        settings = self.app.settings

        def worker() -> None:
            def report(message: str) -> None:
                self.after(0, self.log, f"[{session.title}] {message}")

            try:
                with self.app.jobs.run(session.meeting_id, session.title, jobs.KIND_AFTER_RECORDING) as job:
                    # The Settings in force now, not when the meeting started: unticking the cloud
                    # mid-meeting applies to this meeting.
                    session.stop(on_progress=report, settings=settings, job=job)
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

    def _on_manual_notes_edited(self, _event=None) -> None:
        _highlight_notes(self.manual_notes_editor)
        self._schedule_manual_notes_save()

    def _notes_editable(self) -> bool:
        return str(self.manual_notes_editor.cget("state")) == "normal"

    def _after_notes_format(self) -> str:
        self.manual_notes_editor.focus_set()
        self._on_manual_notes_edited()
        return "break"

    def _toggle_heading(self, level: int) -> str:
        """Makes the current line a heading of this level, or back into plain text if it already is one."""
        if not self._notes_editable():
            return "break"
        editor = self.manual_notes_editor
        line_start = editor.index("insert linestart")
        line = editor.get(line_start, "insert lineend")
        existing = _NOTES_HEADING_MARKER.match(line)
        marker = "#" * level + " "
        editor.edit_separator()
        if existing:
            editor.delete(line_start, f"{line_start}+{len(existing.group(0))}c")
        if existing is None or existing.group(0) != marker:
            # A heading replaces any bullet/indent — "# - x" wouldn't read as either.
            prefix = _NOTES_LIST_MARKER.match(editor.get(line_start, "insert lineend")).group(0)
            if prefix:
                editor.delete(line_start, f"{line_start}+{len(prefix)}c")
            editor.insert(line_start, marker)
        editor.edit_separator()
        return self._after_notes_format()

    def _toggle_inline(self, marker: str) -> str:
        """Wraps the selection in a bold/italic marker, or unwraps it if it's already wrapped. With nothing
        selected, inserts an empty pair with the cursor between, ready to type into."""
        if not self._notes_editable():
            return "break"
        editor = self.manual_notes_editor
        editor.edit_separator()
        if editor.tag_ranges("sel"):
            start, end = editor.index("sel.first"), editor.index("sel.last")
            selected = editor.get(start, end)
            before = editor.get(f"{start}-{len(marker)}c", start)
            after = editor.get(end, f"{end}+{len(marker)}c")
            if len(selected) >= 2 * len(marker) and selected.startswith(marker) and selected.endswith(marker):
                editor.delete(start, end)
                editor.insert(start, selected[len(marker):-len(marker)])
            elif before == marker and after == marker:
                editor.delete(end, f"{end}+{len(marker)}c")
                editor.delete(f"{start}-{len(marker)}c", start)
            else:
                editor.insert(end, marker)
                editor.insert(start, marker)
            editor.tag_remove("sel", "1.0", "end")
        else:
            editor.insert("insert", marker * 2)
            editor.mark_set("insert", f"insert-{len(marker)}c")
        editor.edit_separator()
        return self._after_notes_format()

    def _toggle_bullet(self) -> str:
        """Starts the current line with "- " (after its indent), or takes the bullet off if it has one."""
        if not self._notes_editable():
            return "break"
        editor = self.manual_notes_editor
        line_start = editor.index("insert linestart")
        line = editor.get(line_start, "insert lineend")
        heading = _NOTES_HEADING_MARKER.match(line)
        editor.edit_separator()
        if heading:
            editor.delete(line_start, f"{line_start}+{len(heading.group(0))}c")
            line = line[len(heading.group(0)):]
        indent, bullet = _NOTES_LIST_MARKER.match(line).groups()
        position = f"{line_start}+{len(indent)}c"
        if bullet:
            editor.delete(position, f"{position}+{len(bullet)}c")
        else:
            editor.insert(position, "- ")
        editor.edit_separator()
        return self._after_notes_format()

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
            self._on_manual_notes_edited()
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
            ("Check for minutes", lambda: self.app.check_for_minutes(report=True)),
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
        _configure_notes_tags(self.manual_notes_text, palette, app.fonts)
        self.minutes_text = self._text_tab("Meeting Minutes")
        _configure_notes_tags(self.minutes_text, palette, app.fonts)
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

    def open_meeting(self, meeting_id: int) -> None:
        """Shows one meeting, selecting its project — from the Transcriptions page."""
        meeting = self.app.db.get_meeting(meeting_id)
        if meeting is None:
            return
        self._clear_search()
        self.refresh()  # a meeting recorded since the Library last loaded may be in a new project
        # Selecting the project below reloads the list again once Tk gets to it, re-selecting whichever
        # meeting is current by then — so this one has to be current before that happens.
        self._current = meeting
        for item, project in self._projects.items():
            if project.id == meeting.project_id:
                self.project_tree.selection_set(item)
                self.project_tree.see(item)
        self._reload_meetings(select_id=meeting_id)

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
        _set_readonly_notes(self.manual_notes_text, meeting.manual_notes or "No notes were taken.")
        _set_readonly_notes(self.minutes_text, meeting.minutes or _NO_MINUTES)
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

    def on_minutes_updated(self, meeting_ids: list[int]) -> None:
        """Shows freshly imported minutes if one of those meetings is open."""
        if self._current is not None and self._current.id in meeting_ids:
            self._show_meeting(self._current)

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
            self.local_text, self.cloud_text, self.ocr_text, self.manual_notes_text, self.minutes_text,
            self.attendees_text, self.document_viewer,
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
            kind = jobs.KIND_AGAIN_LOCAL if engine == LOCAL_ENGINE else jobs.KIND_AGAIN_CLOUD
            try:
                with self.app.jobs.run(meeting.id, meeting.title, kind) as job:
                    add_transcription(settings, self.app.db, meeting.id, engine, on_progress=report, job=job)
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
                with self.app.jobs.run(meeting.id, meeting.title, jobs.KIND_RETRY) as job:
                    retry_meeting_transcription(
                        self.app.settings, self.app.db, meeting.id, on_progress=report, job=job
                    )
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


# --- Actions --------------------------------------------------------------------------------------

# The action list's columns: (key, heading, fixed width in pixels — None for a share of what's left).
_ACTION_COLUMNS = (
    ("done", "", 30),
    ("id", "#", 36),
    ("title", "Action", None),
    ("owner", "Owner", None),
    ("status", "Status", 120),
    ("priority", "Priority", 100),
    ("due", "Due", 96),
    ("source", "Source", None),
)
_ACTION_COLUMN_GAP = 10  # between columns
_ACTION_COLUMN_SHARES = {"title": 0.56, "owner": 0.19, "source": 0.25}
_ACTION_COLUMN_NARROW_SHARES = {"title": 0.7, "owner": 0.3, "source": 0.0}
_ACTION_COLUMN_MINIMUMS = {"title": 220, "owner": 100, "source": 130}


def _action_column_widths(total: int) -> dict[str, int]:
    """Each column's width in a list `total` pixels wide: the fixed ones as they are, the rest shared out
    (never below a readable minimum). Too narrow for a Source column, its width is 0 and each action's
    source goes under its title instead."""
    fixed = sum(width for _key, _heading, width in _ACTION_COLUMNS if width is not None)
    spare = total - fixed - _ACTION_COLUMN_GAP * len(_ACTION_COLUMNS)
    wide = spare >= sum(_ACTION_COLUMN_MINIMUMS.values())
    if not wide:
        spare += _ACTION_COLUMN_GAP  # one column fewer
    shares = _ACTION_COLUMN_SHARES if wide else _ACTION_COLUMN_NARROW_SHARES
    widths = {}
    for key, _heading, width in _ACTION_COLUMNS:
        if width is None:
            width = max(_ACTION_COLUMN_MINIMUMS[key], int(max(0, spare) * shares[key])) if shares[key] else 0
        widths[key] = width
    return widths


class ActionsPage(ttk.Frame):
    """Each project's action list — the "<Project> - Actions.json" the weekly run keeps in the project's
    folder under the sync folder — laid out like the browser tracker: category tabs, and a row per action
    to tick off, re-prioritise, annotate and correct, then Save back to the file. See action_tracker.py for
    how a save avoids writing over the weekly run's changes."""

    _POLL_MS = 30_000  # how often to notice the file changing under the page
    _RECORD = "record"  # the category tab showing decisions, items closed this week and gaps
    _FILTER_LABELS = {"all": "All", "mine": "Mine", "flagged": "Flagged", "blocked": "Blocked", "open": "Not done"}
    _NO_FOLDER_TEXT = (
        "Action lists live in the project folders under the Copilot sync folder, as “<Project> - Actions.json”. "
        "Choose the sync folder in Settings to see them here."
    )
    _NO_LISTS_TEXT = "No project folder under {root} has an action list (“<Project> - Actions.json”) yet."

    def __init__(self, parent: tk.Misc, app: MeetingScribeApp):
        super().__init__(parent, padding=(28, 24))
        self.app = app
        palette, fonts = app.palette, app.fonts
        self._badges = badge_colours(palette)
        self._files: dict[str, action_tracker.ActionsFile] = {}  # project folder -> its list
        self._file: action_tracker.ActionsFile | None = None
        self._loaded: action_tracker.LoadedActions | None = None
        self._conflict: action_tracker.LoadedActions | None = None
        self._dirty = False
        self._changed_ids: set = set()  # actions changed since the list was loaded — what a merge carries over
        self._category: str | None = None  # None: every category; _RECORD: decisions, closed and gaps
        self._quick_filter = "all"
        self._adding = False
        self._scan_running = False
        self._search_after_id: str | None = None
        self._resize_after_id: str | None = None
        self._widths = _action_column_widths(900)
        self._wrapped: list[tuple[tk.Widget, str, int]] = []  # (label, column, margin) to re-wrap on resize
        self._notes: list[tuple[dict, tk.Text]] = []  # each shown action's notes box
        self._row_vars: list[tk.Variable] = []  # kept alive: Tk forgets a variable once Python does
        self._rows: dict[int, tuple[int, list[tk.Widget]]] = {}  # id(action) -> (grid row, its widgets)
        self._row_parts: list[tk.Widget] = []  # the widgets of the row being drawn
        self._generation = 0  # bumped by each redraw, so a stale batch of rows stops drawing

        self._title_font = tkfont.Font(font=fonts.strong)
        self._done_font = tkfont.Font(font=fonts.strong)
        self._done_font.configure(overstrike=True)
        self._badge_font = tkfont.Font(font=fonts.small)
        self._badge_font.configure(weight="bold")
        self._heading_font = tkfont.Font(font=fonts.small)
        self._heading_font.configure(weight="bold")
        style = ttk.Style(self)
        style.configure("Select.TButton", anchor="w", padding=(10, 4), font=fonts.body)

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Actions", style="Heading.TLabel").pack(side="left")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(header, textvariable=self.project_var, state="readonly", width=30)
        self.project_combo.pack(side="left", padx=(18, 0))
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_chosen)
        self.search = _PlaceholderEntry(header, "Search actions, owners, notes…", width=34)
        self.search.pack(side="right")
        self.search.var.trace_add("write", self._on_search_changed)
        self.search.bind("<Escape>", lambda _e: self.search.var.set(""))

        # Which file, whether it's saved, and what can be done with it.
        status_card, status = _card(self, padding=(14, 8, 8, 8))
        status_card.pack(fill="x")
        self.save_button = ttk.Button(status, text="Save", style="Accent.TButton", command=self.save, state="disabled")
        self.save_button.pack(side="right", padx=(8, 0))
        self._file_buttons = []
        for text, command in (
            ("Export CSV…", self._export_csv),
            ("Open folder", self._open_folder),
            ("Reload", self._reload),
        ):
            button = ttk.Button(status, text=text, style="Link.TButton", command=command, state="disabled")
            button.pack(side="right", padx=(4, 0))
            self._file_buttons.append(button)
        self._status_label = ttk.Label(status, style="Card.Muted.TLabel", justify="left")
        self._status_label.pack(side="left", fill="x", expand=True)
        _wrap_to_width(self._status_label, status, margin=420)

        # Shown when the file changed under unsaved changes: how to combine them.
        self._conflict_bar = ttk.Frame(self, style="Card.TFrame", padding=(14, 8))
        self._conflict_label = ttk.Label(self._conflict_bar, style="Banner.TLabel", justify="left")
        self._conflict_label.pack(fill="x")
        _wrap_to_width(self._conflict_label, self._conflict_bar, margin=40)
        conflict_buttons = ttk.Frame(self._conflict_bar, style="Card.TFrame", borderwidth=0)
        conflict_buttons.pack(fill="x", pady=(8, 0))
        for text, button_style, mode in (
            ("Take the new list, keep my changes", "Accent.TButton", "merge"),
            ("Take the new list, drop mine", "TButton", "theirs"),
            ("Overwrite with mine…", "Danger.TButton", "mine"),
        ):
            ttk.Button(conflict_buttons, text=text, style=button_style,
                       command=lambda m=mode: self._resolve_conflict(m)).pack(side="left", padx=(0, 8))

        filters = self._filters_row = ttk.Frame(self)
        filters.pack(fill="x", pady=(12, 0))
        self._filter_buttons: dict[str, ttk.Button] = {}
        for key, label in self._FILTER_LABELS.items():
            button = ttk.Button(filters, text=label, command=lambda k=key: self._set_filter(k))
            button.pack(side="left", padx=(0, 6))
            self._filter_buttons[key] = button
        self._add_button = ttk.Button(filters, text="+ Add action", style="Accent.TButton", command=self._toggle_add,
                                      state="disabled")
        self._add_button.pack(side="left", padx=(10, 0))

        self._empty = ttk.Label(self, style="Muted.TLabel", justify="left")
        _wrap_to_width(self._empty, self, margin=60)

        self._stats_label = ttk.Label(self, style="Muted.TLabel")

        # The category tabs, which scroll sideways when they don't fit.
        self._tabs_area = ttk.Frame(self)
        self._tabs_canvas = tk.Canvas(self._tabs_area, background=palette.background, highlightthickness=0,
                                      borderwidth=0, height=40)
        self._tabs_scroll = ttk.Scrollbar(self._tabs_area, orient="horizontal", command=self._tabs_canvas.xview)
        self._tabs_canvas.configure(xscrollcommand=self._tabs_scroll.set)
        self._tabs = ttk.Frame(self._tabs_canvas)
        self._tabs_canvas.create_window(0, 0, window=self._tabs, anchor="nw")
        self._tabs.bind("<Configure>", lambda _e: self._fit_tabs())
        self._tabs_canvas.bind("<Configure>", lambda _e: self._fit_tabs())
        self._tabs_canvas.pack(fill="x")
        ttk.Separator(self._tabs_area).pack(fill="x")

        # The list: a title bar over a scrolling grid of actions.
        self._list_card, body = _card(self, padding=0)
        top = ttk.Frame(body, style="Card.TFrame", borderwidth=0, padding=(16, 12, 16, 10))
        top.pack(fill="x")
        self._list_title = ttk.Label(top, style="Card.Title.TLabel")
        self._list_title.pack(side="left")
        self._list_count = ttk.Label(top, style="Card.Muted.TLabel")
        self._list_count.pack(side="right")
        ttk.Separator(body).pack(fill="x")
        self._banner = tk.Label(body, background=palette.accent, foreground="#FFFFFF", font=fonts.body,
                                anchor="w", justify="left", padx=16, pady=8)
        _wrap_to_width(self._banner, body, margin=40)
        self._scroller = _ScrollableFrame(body, palette, padding=(10, 0, 10, 10), background=palette.surface,
                                          style="Card.TFrame")
        self._scroller.pack(fill="both", expand=True)
        self._grid = self._scroller.inner
        self._scroller.canvas.bind("<Configure>", self._on_list_resized, add="+")

        self.after(self._POLL_MS, self._poll)
        self._show_state()

    # -- finding and loading the lists ----------------------------------------------------------------

    def on_show(self) -> None:
        self.refresh_projects()
        self._check_file_changed()

    def refresh_projects(self) -> None:
        """Looks for the project folders' action lists, on a background thread since they're on OneDrive."""
        root = self.app.settings.copilot_sync_dir
        if root is None:
            self._files = {}
            self.project_combo["values"] = ()
            self._show_state()
            return
        if self._scan_running:
            return
        self._scan_running = True

        def worker() -> None:
            found = action_tracker.find_action_files(root)
            try:
                self.after(0, self._on_projects_found, found)
            except (RuntimeError, tk.TclError):
                pass  # the window closed while looking

        threading.Thread(target=worker, daemon=True).start()

    def _on_projects_found(self, found: "list[action_tracker.ActionsFile]") -> None:
        self._scan_running = False
        self._files = {f.project: f for f in found}
        self.project_combo["values"] = list(self._files)
        if self._file is not None and self._file.project in self._files:
            if self._files[self._file.project].path != self._file.path and not self._dirty:
                self._open(self._files[self._file.project])  # a conflict copy was renamed back, say
            return
        if self._file is None and self._files:
            recording = self.app.record_page.project_var.get().strip().casefold()
            preferred = next((name for name in self._files if name.casefold() == recording), next(iter(self._files)))
            self._open(self._files[preferred])
        else:
            self._show_state()

    def _on_project_chosen(self, _event=None) -> None:
        chosen = self._files.get(self.project_var.get())
        if chosen is None or (self._file is not None and chosen.path == self._file.path):
            return
        if not self.confirm_discard_changes():
            self.project_var.set(self._file.project if self._file else "")
            return
        self._open(chosen)

    def _open(self, actions_file: "action_tracker.ActionsFile") -> None:
        try:
            loaded = action_tracker.read_action_list(actions_file.path)
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"Couldn't read {actions_file.path.name}: {exc}")
            return
        self._file = actions_file
        self.project_var.set(actions_file.project)
        self._set_loaded(loaded, dirty=False)

    def _set_loaded(self, loaded: "action_tracker.LoadedActions", *, dirty: bool) -> None:
        self._loaded = loaded
        self._conflict = None
        self._dirty = dirty
        self._notes = []  # they belonged to the list being replaced (and were flushed onto it already)
        if not dirty:
            self._changed_ids = set()
        if self._category not in (None, self._RECORD, *action_tracker.categories(loaded.data)):
            self._category = None
        self._show_state()

    def _reload(self) -> None:
        if self._file is None or not self.confirm_discard_changes():
            return
        self._open(self._file)
        self.app.toast(f"Reloaded {self._file.path.name}")

    def _poll(self) -> None:
        try:
            if self.winfo_ismapped():
                self._check_file_changed()
        finally:
            self.after(self._POLL_MS, self._poll)

    def _check_file_changed(self) -> None:
        """Picks up a list the weekly run (or the browser tracker) rewrote while this page had it open — or,
        with unsaved changes here, asks how to combine them."""
        if self._file is None or self._loaded is None or self._conflict is not None:
            return
        self._flush_notes()
        try:
            if action_tracker.file_digest(self._file.path) == self._loaded.digest:
                return
            fresh = action_tracker.read_action_list(self._file.path)
        except (OSError, ValueError):
            return  # mid-sync; try again next time
        if self._dirty:
            self._conflict = fresh
            self._show_state()
        else:
            self._set_loaded(fresh, dirty=False)
            self.app.toast("Picked up a newer action list")

    # -- saving ---------------------------------------------------------------------------------------

    def save(self) -> bool:
        """Writes the list back to its file — unless the file changed since it was loaded, in which case
        nothing is written and the page asks how to combine the two. True if it saved."""
        if self._file is None or self._loaded is None:
            return False
        self._flush_notes()
        try:
            self._loaded = action_tracker.save_action_list(self._file.path, self._loaded)
        except action_tracker.ActionsConflict as conflict:
            self._conflict = conflict.fresh
            self._show_state()
            return False
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save {self._file.path.name}: {exc}")
            return False
        self._dirty = False
        self._changed_ids = set()
        self._show_file_status()
        self.app.toast(f"Saved {self._file.path.name}")
        return True

    def _resolve_conflict(self, mode: str) -> None:
        fresh, loaded = self._conflict, self._loaded
        if fresh is None or loaded is None:
            return
        self._flush_notes()
        if mode == "merge":
            merged, carried = action_tracker.merge_local_onto(fresh, loaded.actions, self._changed_ids)
            self._set_loaded(merged, dirty=True)
            self.app.toast(f"Loaded the newer list and kept {carried} of your change{'' if carried == 1 else 's'} — "
                           "press Save")
        elif mode == "theirs":
            self._set_loaded(fresh, dirty=False)
            self.app.toast("Loaded the newer list; your changes were dropped")
        else:
            if not messagebox.askyesno(
                APP_NAME,
                "Overwrite the file with what's on screen? Anything the weekly run added since you opened it "
                "will be lost.", icon="warning", default="no", parent=self,
            ):
                return
            self._loaded = action_tracker.LoadedActions(loaded.data, fresh.digest)
            self._conflict = None
            if not self.save():
                return
            self._show_state()

    def confirm_discard_changes(self) -> bool:
        """Before the list on screen is replaced (or the app closes): offers to save unsaved changes.
        False means stay put."""
        self._flush_notes()
        if not self._dirty:
            return True
        answer = messagebox.askyesnocancel(
            APP_NAME, f"Save your changes to {self._file.path.name} first?", parent=self
        )
        if answer is None:
            return False
        return self.save() if answer else True

    # -- the page -------------------------------------------------------------------------------------

    def _show_state(self) -> None:
        """Brings every part of the page in line with the loaded list, filters and tab."""
        for widget in (self._empty, self._conflict_bar, self._tabs_area, self._list_card, self._stats_label):
            widget.pack_forget()
        has_list = self._loaded is not None and self._file is not None
        for button in self._file_buttons:
            button.configure(state="normal" if has_list else "disabled")
        self._add_button.configure(state="normal" if has_list else "disabled")
        for key, button in self._filter_buttons.items():
            button.configure(style="Accent.TButton" if key == self._quick_filter else "TButton")
        self._show_file_status()
        if not has_list:
            root = self.app.settings.copilot_sync_dir
            self._empty.configure(text=self._NO_FOLDER_TEXT if root is None else self._NO_LISTS_TEXT.format(root=root))
            self._empty.pack(fill="x", pady=(12, 0))
            return
        if self._conflict is not None:
            self._show_conflict()
            self._conflict_bar.pack(fill="x", pady=(8, 0), before=self._filters_row)
        self._stats_label.pack(side="bottom", fill="x", pady=(10, 0))
        self._tabs_area.pack(fill="x", pady=(10, 12))
        self._list_card.pack(fill="both", expand=True)
        self._render()

    def _show_file_status(self) -> None:
        self.save_button.configure(
            text="Save •" if self._dirty else "Save",
            state="normal" if self._loaded is not None and self._conflict is None else "disabled",
        )
        if self._file is None or self._loaded is None:
            self._status_label.configure(text="No action list open.", style="Card.Muted.TLabel")
            return
        stamp = action_tracker.last_saved(self._loaded.data)
        parts = [f"{self._file.path.name} in the {self._file.project} folder"]
        if stamp:
            parts.append(f"list saved {str(stamp).replace('T', ' ')[:16]}")
        parts.append("unsaved changes — press Save" if self._dirty else "up to date")
        text = "   ·   ".join(parts)
        if self._file.conflict_copy:
            text += (f"\nThis is a OneDrive conflict copy — there's no “{self._file.project}{action_tracker.ACTIONS_SUFFIX}” "
                     "in the folder. Worth renaming it back.")
        self._status_label.configure(text=text, style="Card.Danger.TLabel" if self._dirty else "Card.Muted.TLabel")

    def _show_conflict(self) -> None:
        fresh = self._conflict
        changed = len(self._changed_ids)
        listing = fresh.data.get("list") if isinstance(fresh.data.get("list"), dict) else {}
        generated = listing.get("generated")
        self._conflict_label.configure(text=(
            f"{self._file.path.name} has changed since you opened it — the weekly run has most likely refreshed it. "
            f"It now holds {len(fresh.actions)} actions" + (f", generated {generated}" if generated else "")
            + f". You have {changed} changed action{'' if changed == 1 else 's'} on screen. Nothing has been "
            "written; choose how to combine them."
        ))

    def _visible_actions(self, category: str | None) -> list[dict]:
        return [
            action for action in self._loaded.actions
            if action_tracker.matches(action, category=category, quick_filter=self._quick_filter, query=self.search.value())
        ]

    def _render(self, *, keep_scroll: bool = True) -> None:
        """Redraws the tabs, the list and the totals. The rows are drawn a batch at a time, so the first
        ones show straight away on a long list."""
        position = self._scroller.scroll_position() if keep_scroll else 0.0
        self._flush_notes()
        self._generation += 1
        self._render_tabs()
        self._render_stats()
        for child in self._grid.winfo_children():
            child.destroy()
        self._wrapped, self._notes, self._row_vars, self._rows = [], [], [], {}
        for index, (key, _heading, _width) in enumerate(_ACTION_COLUMNS):
            self._grid.columnconfigure(index, minsize=self._widths[key], weight=0)
        if self._category == self._RECORD:
            self._render_record()
            self._scroller.restore_scroll(position)
        else:
            self._render_actions(position)

    # -- tabs and totals ----------------------------------------------------------------------------

    def _render_tabs(self) -> None:
        palette = self.app.palette
        for child in self._tabs.winfo_children():
            child.destroy()
        tabs = [(None, "All actions")]
        tabs += [(c, action_tracker.category_label(c)) for c in action_tracker.categories(self._loaded.data)]
        tabs.append((self._RECORD, "Decisions, closed & gaps"))
        for category, label in tabs:
            active = category == self._category
            tab = tk.Frame(self._tabs, background=palette.background, cursor="hand2")
            tab.pack(side="left", padx=(0, 4))
            inner = tk.Frame(tab, background=palette.background)
            inner.pack(padx=12, pady=(8, 6))
            parts = [tab, inner, tk.Label(inner, text=label, font=self.app.fonts.strong, background=palette.background,
                                          foreground=palette.text if active else palette.muted)]
            parts[-1].pack(side="left")
            if category != self._RECORD:
                count = len(self._visible_actions(category))
                parts.append(tk.Label(inner, text=f" {count} ", font=self._badge_font, padx=4,
                                      background=palette.accent if active else palette.muted,
                                      foreground=palette.surface if palette.dark else "#FFFFFF"))
                parts[-1].pack(side="left", padx=(6, 0))
            underline = tk.Frame(tab, height=2, background=palette.accent if active else palette.background)
            underline.pack(fill="x")
            parts.append(underline)
            for part in parts:
                part.bind("<Button-1>", lambda _e, c=category: self._choose_category(c))

    def _fit_tabs(self) -> None:
        canvas = self._tabs_canvas
        canvas.configure(scrollregion=canvas.bbox("all"), height=self._tabs.winfo_reqheight())
        overflowing = self._tabs.winfo_reqwidth() > canvas.winfo_width() > 1
        if overflowing and not self._tabs_scroll.winfo_ismapped():
            self._tabs_scroll.pack(fill="x", after=canvas)
        elif not overflowing and self._tabs_scroll.winfo_ismapped():
            self._tabs_scroll.pack_forget()
            canvas.xview_moveto(0)

    def _choose_category(self, category: str | None) -> None:
        if category == self._category:
            return
        self._category = category
        if category == self._RECORD:
            self._adding = False
        self._render(keep_scroll=False)

    def _render_stats(self) -> None:
        counts = action_tracker.summary_counts(self._loaded.actions)
        listing = self._loaded.data.get("list") if isinstance(self._loaded.data.get("list"), dict) else {}
        parts = [
            f"{counts['total']} actions", f"{counts['outstanding']} outstanding", f"{counts['done']} done",
            f"{counts['mine']} mine, outstanding", f"{counts['flagged']} flagged", f"{counts['blocked']} blocked",
            f"{counts['added']} added here",
        ]
        if listing.get("review_slot"):
            parts.append(f"Review: {listing['review_slot']}")
        self._stats_label.configure(text="      ".join(parts))

    def _set_filter(self, key: str) -> None:
        self._quick_filter = "all" if key == self._quick_filter else key
        for name, button in self._filter_buttons.items():
            button.configure(style="Accent.TButton" if name == self._quick_filter else "TButton")
        if self._loaded is not None:
            self._render(keep_scroll=False)

    def _on_search_changed(self, *_args) -> None:
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(250, self._run_search)

    def _run_search(self) -> None:
        self._search_after_id = None
        if self._loaded is not None:
            self._render(keep_scroll=False)

    # -- the list of actions ------------------------------------------------------------------------

    def _on_list_resized(self, event) -> None:
        if self._resize_after_id is not None:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(80, self._apply_widths, event.width - 20)

    def _apply_widths(self, total: int) -> None:
        self._resize_after_id = None
        widths = _action_column_widths(total)
        if widths == self._widths:
            return
        source_moved = bool(widths["source"]) != bool(self._widths["source"])
        self._widths = widths
        if source_moved and self._loaded is not None:
            self._render()
            return
        for index, (key, _heading, _width) in enumerate(_ACTION_COLUMNS):
            self._grid.columnconfigure(index, minsize=widths[key])
        self._wrapped = [(label, column, margin) for label, column, margin in self._wrapped if label.winfo_exists()]
        for label, column, margin in self._wrapped:
            width = total if column == "all" else widths[column]
            label.configure(wraplength=max(60, width - margin))

    def _label(self, parent, text: str, *, column: str | None = None, margin: int = 14, font=None,
               foreground: str | None = None, **options) -> tk.Label:
        """A plain label on the card's surface — wrapped to its column's width when `column` is given."""
        palette = self.app.palette
        label = tk.Label(parent, text=text, font=font or self.app.fonts.body, background=palette.surface,
                         foreground=foreground or palette.text, anchor="w", justify="left", **options)
        if column is not None:
            width = sum(self._widths.values()) if column == "all" else self._widths[column]
            label.configure(wraplength=max(60, width - margin))
            self._wrapped.append((label, column, margin))
        return label

    def _badge(self, parent, text: str, kind: str) -> tk.Label:
        background, foreground = self._badges[kind]
        return tk.Label(parent, text=text.upper(), font=self._badge_font, background=background,
                        foreground=foreground, padx=7, pady=1)

    def _link(self, parent, text: str, command, *, tooltip: str | None = None) -> tk.Label:
        """A small clickable glyph (✎, ⋯, ×) that doesn't look like a button."""
        palette = self.app.palette
        link = tk.Label(parent, text=text, font=self.app.fonts.body, background=palette.surface,
                        foreground=palette.muted, cursor="hand2", padx=3)
        link.bind("<Button-1>", lambda _e: command())
        link.bind("<Enter>", lambda _e: link.configure(foreground=palette.accent))
        link.bind("<Leave>", lambda _e: link.configure(foreground=palette.muted))
        return link

    def _cell(self, row: int, column: str, widget: tk.Widget | None = None, **grid) -> tk.Widget:
        """Puts `widget` (or a new frame for several) in one of the row's columns, as part of the row."""
        index = [k for k, _h, _w in _ACTION_COLUMNS].index(column)
        cell = widget if widget is not None else ttk.Frame(self._grid, style="Card.TFrame", borderwidth=0)
        gap = 0 if column == "due" and not self._widths["source"] else _ACTION_COLUMN_GAP
        cell.grid(row=row, column=index, sticky=grid.pop("sticky", "new"), padx=(0, gap), pady=(10, 12), **grid)
        self._row_parts.append(cell)
        return cell

    def _render_list_header(self) -> None:
        """The list's title, how many actions it shows, and the flagged/blocked banner."""
        rows = self._visible_actions(self._category)
        done = sum(1 for a in rows if a.get("status") == "closed")
        self._list_title.configure(
            text=action_tracker.category_label(self._category) if self._category else "All actions"
        )
        self._list_count.configure(text=f"{len(rows)} shown · {done} done")
        counts = action_tracker.summary_counts(self._loaded.actions)
        order = self._loaded.data.get("priority_order_agreed") or []
        if counts["flagged"] or counts["blocked"]:
            banner = f"{counts['flagged']} flagged and {counts['blocked']} blocked"
            if order:
                banner += " — priority order agreed at the weekly is " + " → ".join(str(o) for o in order)
            self._banner.configure(text=banner + ".")
            if not self._banner.winfo_ismapped():
                self._banner.pack(fill="x", before=self._scroller)
        else:
            self._banner.pack_forget()

    _BATCH = 12  # rows drawn at a time

    def _render_actions(self, position: float) -> None:
        palette = self.app.palette
        self._render_list_header()
        rows = sorted(self._visible_actions(self._category), key=action_tracker.sort_key)
        row = 0
        if self._adding:
            self._render_add_form(row)
            row += 1
        for index, (key, heading, _width) in enumerate(_ACTION_COLUMNS):
            if self._widths[key]:
                self._label(self._grid, heading.upper(), font=self._heading_font, foreground=palette.muted).grid(
                    row=row, column=index, sticky="w", pady=(10, 6)
                )
        ttk.Separator(self._grid).grid(row=row + 1, column=0, columnspan=len(_ACTION_COLUMNS), sticky="ew")
        row += 2
        if not rows:
            self._label(self._grid, "Nothing matches those filters.", foreground=palette.muted).grid(
                row=row, column=0, columnspan=len(_ACTION_COLUMNS), sticky="w", pady=24, padx=10
            )
            self._scroller.restore_scroll(0.0)
            return
        placed = [(action, row + 2 * index) for index, action in enumerate(rows)]
        self._render_batch(self._generation, placed, 0, position)

    def _render_batch(self, generation: int, placed: list, start: int, position: float) -> None:
        if generation != self._generation:
            return  # redrawn since
        today = datetime.now().date()
        for action, row in placed[start:start + self._BATCH]:
            self._render_row(action, row, today)
        if start + self._BATCH < len(placed):
            self.after(1, self._render_batch, generation, placed, start + self._BATCH, position)
        if start == 0 or start + self._BATCH >= len(placed):
            self._scroller.restore_scroll(position if start + self._BATCH >= len(placed) else 0.0)

    def _render_row(self, action: dict, row: int, today) -> None:
        palette = self.app.palette
        status, priority = action.get("status"), action.get("priority")
        done = status == "closed"
        self._row_parts = []

        done_var = tk.BooleanVar(value=done)
        self._row_vars.append(done_var)
        self._cell(row, "done", ttk.Checkbutton(
            self._grid, variable=done_var, style="Card.TCheckbutton",
            command=lambda: self._set_done(action, done_var.get()),
        ), sticky="nw")
        self._cell(row, "id", self._label(self._grid, str(action.get("id", "")), font=self.app.fonts.mono,
                                          foreground=palette.muted), sticky="nw")
        self._fill_title_cell(self._cell(row, "title"), action)
        self._fill_text_cell(self._cell(row, "owner"), action, "owner")
        self._cell(row, "status", self._select(action_tracker.STATUSES, action_tracker.STATUS_LABELS, status,
                                               lambda value: self._set_field(action, "status", value)))
        self._cell(row, "priority", self._select(action_tracker.PRIORITIES, action_tracker.PRIORITY_LABELS, priority,
                                                 lambda value: self._set_field(action, "priority", value)))
        self._fill_text_cell(self._cell(row, "due"), action, "due", today=today)

        if self._widths["source"]:
            source_cell = self._cell(row, "source")
            source = action.get("source") if isinstance(action.get("source"), dict) else {}
            self._badge(source_cell, str(source.get("type") or "—"), "neutral").pack(anchor="w")
            details = "\n".join(str(part) for part in (source.get("date"), source.get("detail")) if part)
            if details:
                self._label(source_cell, details, column="source", font=self.app.fonts.small,
                            foreground=palette.muted).pack(anchor="w", pady=(4, 0))

        separator = ttk.Separator(self._grid)
        separator.grid(row=row + 1, column=0, columnspan=len(_ACTION_COLUMNS), sticky="ew")
        self._row_parts.append(separator)
        self._rows[id(action)] = (row, self._row_parts)

    def _refresh_row(self, action: dict) -> None:
        """Redraws one action's row after a change — or takes it out of the list if it no longer belongs
        under the current tab and filters — and the totals, without redrawing the whole list."""
        self._flush_notes()
        placed = self._rows.pop(id(action), None)
        if placed is None:
            self._render()
            return
        row, widgets = placed
        for widget in widgets:
            widget.destroy()
        self._notes = [(a, box) for a, box in self._notes if a is not action]
        if action_tracker.matches(action, category=self._category, quick_filter=self._quick_filter,
                                  query=self.search.value()):
            self._render_row(action, row, datetime.now().date())
        self._render_tabs()
        self._render_stats()
        self._render_list_header()

    def _fill_title_cell(self, cell: ttk.Frame, action: dict, *, editing: bool = False) -> None:
        palette = self.app.palette
        self._flush_notes()  # its notes box is about to be replaced
        for child in cell.winfo_children():
            child.destroy()
        status, priority = action.get("status"), action.get("priority")
        done = status == "closed"
        if editing:
            self._inline_editor(cell, action, "title")
        else:
            line = ttk.Frame(cell, style="Card.TFrame", borderwidth=0)
            line.pack(fill="x", anchor="w")
            title = self._label(line, str(action.get("title") or ""), column="title", margin=42,
                                font=self._done_font if done else self._title_font,
                                foreground=palette.muted if done else palette.text)
            title.pack(side="left", anchor="nw")
            self._link(line, "✎", lambda: self._fill_title_cell(cell, action, editing=True)).pack(side="left", anchor="nw")
            more = self._link(line, "⋯", lambda: None)
            more.bind("<Button-1>", lambda _e: self._more_menu(more, action))
            more.pack(side="left", anchor="nw")

        badges = [
            ("Mine", "mine", action.get("is_mine")),
            ("Flagged", "flagged", priority == "flagged"),
            ("In progress", "in_progress", status == "in_progress"),
            ("Blocked", "blocked", status == "blocked"),
            ("Done", "done", done),
            ("Added here", "neutral", action.get("user_added")),
        ]
        shown = [(text, kind) for text, kind, on in badges if on]
        if shown:
            line = ttk.Frame(cell, style="Card.TFrame", borderwidth=0)
            line.pack(fill="x", pady=(4, 0))
            for text, kind in shown:
                self._badge(line, text, kind).pack(side="left", padx=(0, 5))

        source = action.get("source") if isinstance(action.get("source"), dict) else {}
        if not self._widths["source"] and any(source.values()):
            where = " · ".join(str(part) for part in (source.get("type"), source.get("date"), source.get("detail")) if part)
            self._label(cell, f"Source: {where}", column="title", margin=10, font=self.app.fonts.small,
                        foreground=palette.muted).pack(anchor="w", pady=(4, 0))

        notes = tk.Text(cell, height=2, width=10, wrap="word", font=self.app.fonts.small, relief="flat",
                        borderwidth=0, highlightthickness=0, padx=2, pady=2, background=palette.surface,
                        foreground=palette.muted, insertbackground=palette.text,
                        selectbackground=palette.accent_soft, selectforeground=palette.text)
        notes.insert("1.0", action.get("notes") or "")
        notes.pack(fill="x", pady=(6, 0))
        notes.bind("<FocusIn>", lambda _e: notes.configure(background=palette.background))
        notes.bind("<FocusOut>", lambda _e: (notes.configure(background=palette.surface), self._flush_notes()))
        self._notes = [(a, t) for a, t in self._notes if a is not action]
        self._notes.append((action, notes))

        for index, entry in enumerate(action.get("log") or []):
            line = ttk.Frame(cell, style="Card.TFrame", borderwidth=0)
            line.pack(fill="x", pady=(4, 0))
            when = action_tracker.log_date(entry)
            text = action_tracker.log_text(entry)
            self._label(line, f"{when}  ·  {text}" if when else text, column="title", margin=24,
                        font=self.app.fonts.small).pack(side="left", anchor="nw")
            self._link(line, "×", lambda i=index: self._remove_log_entry(action, i)).pack(side="left", anchor="nw")
        ttk.Button(cell, text="＋ note", style="Link.TButton", command=lambda: self._add_log_entry(action)).pack(
            anchor="w", pady=(4, 0)
        )

    def _fill_text_cell(self, cell: ttk.Frame, action: dict, field: str, *, today=None, editing: bool = False) -> None:
        """The owner or due-date cell: its value and a ✎ to change it."""
        palette = self.app.palette
        for child in cell.winfo_children():
            child.destroy()
        if editing:
            self._inline_editor(cell, action, field)
            return
        line = ttk.Frame(cell, style="Card.TFrame", borderwidth=0)
        line.pack(fill="x", anchor="w")
        if field == "due":
            soon = action_tracker.is_due_soon(action, today or datetime.now().date())
            self._label(line, action.get("due") or "—", font=self.app.fonts.strong if soon else self.app.fonts.body,
                        foreground=palette.danger if soon else palette.text).pack(side="left", anchor="nw")
        else:
            self._label(line, str(action.get("owner") or ""), column="owner", margin=34).pack(side="left", anchor="nw")
        self._link(line, "✎", lambda: self._fill_text_cell(cell, action, field, editing=True)).pack(side="left", anchor="nw")

    def _inline_editor(self, cell: ttk.Frame, action: dict, field: str) -> None:
        """Swaps a cell's value for an entry: Enter (or Save) keeps the change, Esc (or Cancel) doesn't."""
        palette = self.app.palette
        var = tk.StringVar(value=action.get(field) or ("" if field != "owner" else ""))
        self._row_vars.append(var)
        entry = ttk.Entry(cell, textvariable=var, width=4)  # as wide as the column, not wider
        entry.pack(fill="x")
        buttons = ttk.Frame(cell, style="Card.TFrame", borderwidth=0)
        buttons.pack(fill="x", pady=(4, 0))

        def redraw() -> None:
            if field == "title":
                self._fill_title_cell(cell, action)
            else:
                self._fill_text_cell(cell, action, field)

        def commit() -> None:
            value = var.get().strip()
            if field == "title" and not value:
                self.app.toast("An action needs a title")
                return
            if field == "owner":
                value = value or "Unassigned"
            if field == "due":
                if value and not _is_iso_date(value):
                    self.app.toast("Give the due date as YYYY-MM-DD, e.g. 2026-10-02")
                    return
                value = value or None
            if action.get(field) != value:
                action[field] = value
                self._changed(action, edited=True)
                self.app.toast(f"Updated the {'due date' if field == 'due' else field} of action {action.get('id')}")
            redraw()

        ttk.Button(buttons, text="Save", style="Link.TButton", command=commit).pack(side="left")
        ttk.Button(buttons, text="Cancel", style="Link.TButton", command=redraw).pack(side="left")
        if field == "due":
            self._label(cell, "YYYY-MM-DD", font=self.app.fonts.small, foreground=palette.muted).pack(anchor="w")
        entry.bind("<Return>", lambda _e: commit())
        entry.bind("<Escape>", lambda _e: (redraw(), "break")[1])
        entry.focus_set()
        entry.select_range(0, "end")

    def _select(self, values, labels: dict, current, on_pick) -> ttk.Button:
        """A dropdown-looking button that pops up the choices — lighter than a combobox per row, and the
        mouse wheel scrolls the list over it rather than changing its value."""
        button = ttk.Button(self._grid, text=f"{labels.get(current, current or '—')}  ▾", style="Select.TButton", width=12)
        button.configure(command=lambda: self._popup_choices(button, values, labels, current, on_pick))
        return button

    def _menu(self) -> tk.Menu:
        palette = self.app.palette
        return tk.Menu(self, tearoff=0, background=palette.surface, foreground=palette.text,
                       activebackground=palette.accent_soft, activeforeground=palette.text,
                       selectcolor=palette.accent, font=self.app.fonts.body, borderwidth=1, relief="solid")

    def _popup_choices(self, widget: tk.Widget, values, labels: dict, current, on_pick) -> None:
        menu = self._menu()
        var = tk.StringVar(value=current or "")
        menu.var = var  # kept alive while the menu is
        for value in values:
            menu.add_radiobutton(label=labels[value], value=value, variable=var, command=lambda v=value: on_pick(v))
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _more_menu(self, widget: tk.Widget, action: dict) -> None:
        """Whose the action is and its category — which the list's columns don't show."""
        menu = self._menu()
        mine = tk.BooleanVar(value=bool(action.get("is_mine")))
        category = tk.StringVar(value=action.get("category") or "")
        menu.vars = (mine, category)
        menu.add_checkbutton(label="Mine", variable=mine,
                             command=lambda: self._set_field(action, "is_mine", mine.get(), edited=True))
        moves = tk.Menu(menu, tearoff=0, background=menu.cget("background"), foreground=menu.cget("foreground"),
                        activebackground=menu.cget("activebackground"), activeforeground=menu.cget("activeforeground"),
                        selectcolor=menu.cget("selectcolor"), font=self.app.fonts.body)
        for key in action_tracker.categories(self._loaded.data):
            moves.add_radiobutton(label=action_tracker.category_label(key), value=key, variable=category,
                                  command=lambda k=key: self._set_field(action, "category", k, edited=True))
        menu.add_cascade(label="Category", menu=moves)
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _render_add_form(self, row: int) -> None:
        palette = self.app.palette
        form = ttk.Frame(self._grid, style="Card.TFrame", borderwidth=0, padding=(6, 12, 6, 12))
        form.grid(row=row, column=0, columnspan=len(_ACTION_COLUMNS), sticky="ew")
        form.columnconfigure(0, weight=3)
        form.columnconfigure(1, weight=1)
        categories = action_tracker.categories(self._loaded.data)
        default_category = self._category if self._category in categories else (categories[0] if categories else "")
        title, owner, due = tk.StringVar(), tk.StringVar(), tk.StringVar()
        category = tk.StringVar(value=action_tracker.category_label(default_category))
        priority = tk.StringVar(value=action_tracker.PRIORITY_LABELS["normal"])
        mine = tk.StringVar(value="Yes, mine")
        self._row_vars.extend((title, owner, due, category, priority, mine))

        def field(text: str, var: tk.StringVar, row_: int, column: int, **entry_options) -> ttk.Entry:
            box = ttk.Frame(form, style="Card.TFrame", borderwidth=0)
            box.grid(row=row_, column=column, sticky="ew", padx=(0, 10), pady=(0, 8))
            ttk.Label(box, text=text, style="Card.Muted.TLabel").pack(anchor="w")
            entry = ttk.Entry(box, textvariable=var, **entry_options)
            entry.pack(fill="x")
            return entry

        title_entry = field("Action", title, 0, 0, width=10)
        field("Owner", owner, 0, 1, width=16)
        field("Due (YYYY-MM-DD)", due, 0, 2, width=14)
        second = ttk.Frame(form, style="Card.TFrame", borderwidth=0)
        second.grid(row=1, column=0, columnspan=3, sticky="w")
        for text, var, values, width in (
            ("Category", category, [action_tracker.category_label(c) for c in categories], 22),
            ("Priority", priority, [action_tracker.PRIORITY_LABELS[p] for p in action_tracker.PRIORITIES], 9),
            ("Mine?", mine, ["Yes, mine", "Someone else"], 12),
        ):
            box = ttk.Frame(second, style="Card.TFrame", borderwidth=0)
            box.pack(side="left", padx=(0, 10))
            ttk.Label(box, text=text, style="Card.Muted.TLabel").pack(anchor="w")
            ttk.Combobox(box, textvariable=var, values=values, state="readonly", width=width).pack()

        def add() -> None:
            text = title.get().strip()
            if not text:
                self.app.toast("Give the action a title first")
                return
            when = due.get().strip()
            if when and not _is_iso_date(when):
                self.app.toast("Give the due date as YYYY-MM-DD, e.g. 2026-10-02")
                return
            by_label = {action_tracker.category_label(c): c for c in categories}
            action = action_tracker.new_action(
                self._loaded.actions, title=text, category=by_label.get(category.get(), default_category),
                today=datetime.now().date(), owner=owner.get().strip() or "Unassigned",
            )
            action["due"] = when or None
            action["priority"] = {v: k for k, v in action_tracker.PRIORITY_LABELS.items()}[priority.get()]
            action["is_mine"] = mine.get() == "Yes, mine"
            self._loaded.actions.append(action)
            self._adding = False
            self._changed(action)
            self._render()
            self.app.toast(f"Added action {action['id']}")

        ttk.Button(second, text="Add action", style="Accent.TButton", command=add).pack(side="left", padx=(6, 6), pady=(16, 0))
        ttk.Button(second, text="Cancel", command=self._toggle_add).pack(side="left", pady=(16, 0))
        hint = self._label(form, "Actions added here are marked “Added here” and saved into the list with the rest.",
                           column="all", margin=40, font=self.app.fonts.small, foreground=palette.muted)
        hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        title_entry.bind("<Return>", lambda _e: add())
        title_entry.focus_set()
        ttk.Separator(form).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def _render_record(self) -> None:
        """Decisions recorded this week, items closed this week and the known gaps in the list, as cards."""
        palette = self.app.palette
        self._list_title.configure(text="Decisions, closed & gaps")
        self._list_count.configure(text="")
        self._banner.pack_forget()
        data = self._loaded.data
        sections = (
            ("Decisions recorded this week", [
                (d.get("decision"), d.get("rationale"),
                 " · ".join(str(p) for p in ((d.get("source") or {}).get("type"), (d.get("source") or {}).get("date")) if p))
                for d in data.get("decisions_this_week") or [] if isinstance(d, dict)
            ]),
            ("Closed this week", [
                (c.get("title"), c.get("evidence"), "") for c in data.get("closed_this_week") or [] if isinstance(c, dict)
            ]),
            ("Gaps in this list", [(None, str(g), "") for g in data.get("gaps") or []]),
        )
        row = 0
        for heading, items in sections:
            top = ttk.Frame(self._grid, style="Card.TFrame", borderwidth=0)
            top.grid(row=row, column=0, columnspan=len(_ACTION_COLUMNS), sticky="ew", pady=(16, 6), padx=6)
            self._label(top, heading, font=self.app.fonts.title).pack(side="left")
            self._label(top, str(len(items)), foreground=palette.muted).pack(side="right")
            ttk.Separator(self._grid).grid(row=row + 1, column=0, columnspan=len(_ACTION_COLUMNS), sticky="ew")
            row += 2
            for bold, text, source in items:
                box = ttk.Frame(self._grid, style="Card.TFrame", borderwidth=0)
                box.grid(row=row, column=0, columnspan=len(_ACTION_COLUMNS), sticky="ew", pady=(8, 8), padx=6)
                if bold:
                    self._label(box, str(bold), column="all", margin=40, font=self._title_font).pack(anchor="w")
                if text:
                    self._label(box, str(text), column="all", margin=40,
                                foreground=palette.text if bold is None else palette.muted).pack(anchor="w", pady=(2, 0))
                if source:
                    self._label(box, source, font=self.app.fonts.small, foreground=palette.muted).pack(anchor="w", pady=(2, 0))
                row += 1
            if not items:
                self._label(self._grid, "None recorded.", foreground=palette.muted).grid(
                    row=row, column=0, columnspan=len(_ACTION_COLUMNS), sticky="w", padx=6, pady=8
                )
                row += 1

    # -- changing actions -------------------------------------------------------------------------------

    def _changed(self, action: dict, *, edited: bool = False) -> None:
        """Marks an action as changed here — which a later merge onto a newer list relies on — and the list
        as unsaved."""
        action["touched"] = True
        if edited:
            action["edited"] = True
        self._changed_ids.add(action.get("id"))
        if not self._dirty:
            self._dirty = True
            self._show_file_status()

    def _flush_notes(self) -> None:
        """Copies any notes typed into the list onto their actions."""
        for action, box in self._notes:
            try:
                text = box.get("1.0", "end-1c")
            except tk.TclError:
                continue  # already gone
            if text != (action.get("notes") or ""):
                action["notes"] = text
                self._changed(action)

    def _set_field(self, action: dict, field: str, value, *, edited: bool = False) -> None:
        if action.get(field) == value:
            return
        action[field] = value
        self._changed(action, edited=edited)
        self._refresh_row(action)

    def _set_done(self, action: dict, done: bool) -> None:
        self._set_field(action, "status", "closed" if done else "open")
        self.app.toast(f"Action {action.get('id')} {'marked done' if done else 'reopened'}")

    def _toggle_add(self) -> None:
        if self._loaded is None:
            return
        self._adding = not self._adding
        if self._adding and self._category == self._RECORD:
            self._category = None
        self._render(keep_scroll=not self._adding)

    def _add_log_entry(self, action: dict) -> None:
        text = simpledialog.askstring(APP_NAME, f"Add a short note to action {action.get('id')}:", parent=self)
        if not text or not text.strip():
            return
        action.setdefault("log", []).append({"date": datetime.now().date().isoformat(), "text": text.strip()})
        self._changed(action)
        self._refresh_row(action)
        self.app.toast(f"Note added to action {action.get('id')}")

    def _remove_log_entry(self, action: dict, index: int) -> None:
        del action["log"][index]
        self._changed(action)
        self._refresh_row(action)

    # -- file actions ---------------------------------------------------------------------------------------

    def _open_folder(self) -> None:
        if self._file is None:
            return
        try:
            _open_path(self._file.path.parent)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't open {self._file.path.parent}: {exc}")

    def _export_csv(self) -> None:
        if self._loaded is None or self._file is None:
            return
        self._flush_notes()
        path = filedialog.asksaveasfilename(
            title="Export actions", defaultextension=".csv", initialfile=f"{self._file.project} - Actions.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        Path(path).write_text(action_tracker.to_csv(self._loaded.actions), encoding="utf-8-sig", newline="")
        self.app.toast(f"Exported to {Path(path).name}")


def _is_iso_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


# --- Transcriptions -------------------------------------------------------------------------------


class _JobRow:
    """One running transcription on the Transcriptions page: name, what it's doing, and a progress bar —
    measured while transcribing on this PC, a moving bar while it waits or runs in the cloud."""

    def __init__(self, parent: ttk.Frame):
        self.frame = ttk.Frame(parent, style="Card.TFrame", borderwidth=0)
        self.frame.columnconfigure(0, weight=1)
        self._title = ttk.Label(self.frame, style="Card.TLabel")
        self._title.grid(row=0, column=0, sticky="w")
        self._kind = ttk.Label(self.frame, style="Card.Muted.TLabel")
        self._kind.grid(row=0, column=1, sticky="e")
        self._bar = ttk.Progressbar(self.frame, style="Progress.Horizontal.TProgressbar", maximum=100)
        self._bar.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 2))
        self._detail = ttk.Label(self.frame, style="Card.Muted.TLabel")
        self._detail.grid(row=2, column=0, columnspan=2, sticky="w")
        self._message = ttk.Label(self.frame, style="Card.Muted.TLabel", justify="left")
        self._message.grid(row=3, column=0, columnspan=2, sticky="w")
        _wrap_to_width(self._message, self.frame, margin=10)
        self._moving = False

    def show(self, job: JobSnapshot) -> None:
        self._title.configure(text=job.title)
        self._kind.configure(text=job.kind)
        self._detail.configure(text=_job_progress_text(job))
        self._message.configure(text=job.message)
        measured = job.fraction is not None and job.stage == jobs.LOCAL
        if measured:
            if self._moving:
                self._bar.stop()
                self._moving = False
            self._bar.configure(mode="determinate", value=job.fraction * 100)
        elif not self._moving:
            self._bar.configure(mode="indeterminate", value=0)
            self._bar.start(20)
            self._moving = True

    def destroy(self) -> None:
        if self._moving:
            self._bar.stop()
        self.frame.destroy()


class TranscriptionsPage(ttk.Frame):
    """What's being transcribed now and how far along it is, and a log of every meeting and each time it
    was transcribed — after recording, retried, or again afterwards on this PC or in the cloud."""

    _POLL_MS = 1000
    _MEETINGS_SHOWN = 200
    _IDLE_TEXT = (
        "Nothing is being transcribed right now. A meeting shows up here as soon as it's stopped, "
        "with how far along this PC is and roughly how long it has to go."
    )

    def __init__(self, parent: tk.Misc, app: MeetingScribeApp):
        super().__init__(parent, padding=(28, 24))
        self.app = app
        self._rows: dict[int, _JobRow] = {}
        self._active_ids: tuple[int, ...] = ()
        self._meeting_items: dict[str, int] = {}  # tree item -> meeting id
        self._run_items: dict[str, object] = {}  # tree item -> transcription run row

        ttk.Label(self, text="Transcriptions", style="Heading.TLabel").pack(anchor="w", pady=(0, 16))

        now_card, self._now = _card(self, "Now")
        now_card.pack(fill="x")
        self._idle = ttk.Label(self._now, text=self._IDLE_TEXT, style="Card.Muted.TLabel", justify="left")
        self._idle.pack(anchor="w", fill="x")
        _wrap_to_width(self._idle, self._now, margin=20)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(12, 0))

        history_card, history = _card(panes, padding=0)
        panes.add(history_card, weight=3)
        history_header = ttk.Frame(history, style="Card.TFrame", borderwidth=0, padding=(14, 10, 8, 4))
        history_header.pack(fill="x")
        ttk.Label(history_header, text="Meetings", style="Card.Title.TLabel").pack(side="left")
        self._open_button = ttk.Button(
            history_header, text="Open in Library", style="Link.TButton", state="disabled",
            command=self._open_selected,
        )
        self._open_button.pack(side="right")
        tree_frame = ttk.Frame(history, style="Card.TFrame", borderwidth=0)
        tree_frame.pack(fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        self.tree = ttk.Treeview(tree_frame, columns=("when", "status", "took"), show="tree headings", selectmode="browse")
        for column, heading, width, stretch in (
            ("#0", "Meeting", 190, True),
            ("when", "When", 120, False),
            ("status", "Transcription", 170, True),
            ("took", "Duration", 70, False),
        ):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=width, stretch=stretch, anchor="w")
        self.tree.tag_configure("problem", foreground=app.palette.danger)
        self.tree.tag_configure("run", foreground=app.palette.muted)
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_selected)
        self.tree.bind("<Double-1>", lambda _e: self._open_selected())

        log_card, log = _card(panes, padding=0)
        panes.add(log_card, weight=2)
        log_header = ttk.Frame(log, style="Card.TFrame", borderwidth=0, padding=(14, 10, 8, 4))
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Log", style="Card.Title.TLabel").pack(side="left")
        log_frame, self.log_text = _scrolled_text(log, app.palette, app.fonts.mono, state="disabled", width=30)
        self.log_text.configure(foreground=app.palette.muted)
        log_frame.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 2))
        _set_readonly_text(self.log_text, "Pick a meeting or one of its transcriptions to see what happened.")

        self.after(self._POLL_MS, self._poll)

    def on_show(self) -> None:
        self._refresh_now()
        self.refresh_history()

    # -- now --------------------------------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            if self.winfo_ismapped():
                self._refresh_now()
            else:
                self._active_ids = tuple(job.id for job in self.app.jobs.active())
        finally:
            self.after(self._POLL_MS, self._poll)

    def _refresh_now(self) -> None:
        active = self.app.jobs.active()
        ids = tuple(job.id for job in active)
        if ids != self._active_ids:
            # A job started or finished: the history changes with it.
            self._active_ids = ids
            self.refresh_history()
        for job_id in [job_id for job_id in self._rows if job_id not in ids]:
            self._rows.pop(job_id).destroy()
        for index, job in enumerate(active):
            row = self._rows.get(job.id)
            if row is None:
                row = self._rows[job.id] = _JobRow(self._now)
            row.frame.pack(fill="x", pady=(0 if index == 0 else 12, 0))
            row.show(job)
        if active:
            self._idle.pack_forget()
        else:
            self._idle.pack(anchor="w", fill="x")
        self._refresh_selected_log()

    # -- history ----------------------------------------------------------------------------------------

    def refresh_history(self) -> None:
        """Reloads the meeting log, keeping the selection and which meetings are expanded."""
        selected = self.tree.selection()
        selected_key = self._item_key(selected[0]) if selected else None
        expanded = {self._meeting_items[item] for item in self._meeting_items if self.tree.item(item, "open")}
        db = self.app.db
        runs: dict[int, list] = {}
        for run in db.list_transcription_runs():
            runs.setdefault(run["meeting_id"], []).append(run)
        engines = db.transcribed_engines()
        live = self._jobs_by_run()
        busy = {job.meeting_id for job in live.values() if job.active}

        self.tree.delete(*self.tree.get_children())
        self._meeting_items, self._run_items = {}, {}
        reselect = None
        for meeting in db.list_recent_meetings(self._MEETINGS_SHOWN):
            in_progress = meeting.id in busy or meeting_in_progress(meeting.id)
            status = _meeting_transcription_status(meeting, engines.get(meeting.id, set()), in_progress)
            problem = meeting.ended_at is None and not in_progress
            item = self.tree.insert(
                "", "end", text="  " + meeting.title,
                values=(_format_short_date(meeting.started_at), status, _format_duration(meeting.started_at, meeting.ended_at)),
                open=meeting.id in expanded, tags=("problem",) if problem else (),
            )
            self._meeting_items[item] = meeting.id
            if selected_key == ("meeting", meeting.id):
                reselect = item
            for run in runs.get(meeting.id, []):
                outcome, _detail = _run_outcome(run, live.get(run["id"]))
                child = self.tree.insert(
                    item, "end", text=f"    {run['kind']}",
                    values=(_format_run_time(run["started_at"]), outcome, _run_took(run)),
                    tags=("problem",) if outcome in ("Failed", "Interrupted") else ("run",),
                )
                self._run_items[child] = run
                if selected_key == ("run", run["id"]):
                    reselect = child
        if reselect is not None:
            self.tree.selection_set(reselect)
            self.tree.see(reselect)
        else:
            self._open_button.configure(state="disabled")

    def _jobs_by_run(self) -> "dict[int, JobSnapshot]":
        return {job.run_id: job for job in self.app.jobs.snapshot() if job.run_id is not None}

    def _item_key(self, item: str):
        if item in self._meeting_items:
            return ("meeting", self._meeting_items[item])
        if item in self._run_items:
            return ("run", self._run_items[item]["id"])
        return None

    def _selected_meeting_id(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        item = selection[0]
        if item in self._run_items:
            return self._run_items[item]["meeting_id"]
        return self._meeting_items.get(item)

    def _on_selected(self, _event=None) -> None:
        self._open_button.configure(state="normal" if self._selected_meeting_id() is not None else "disabled")
        self._refresh_selected_log(force=True)

    def _refresh_selected_log(self, *, force: bool = False) -> None:
        """Shows the selected run's log, or every run of the selected meeting — kept current while one of
        them is still running."""
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        live = self._jobs_by_run()
        if item in self._run_items:
            runs = [self._run_items[item]]
        elif item in self._meeting_items:
            runs = [self._run_items[child] for child in self.tree.get_children(item)]
        else:
            return
        if not force and not any(run["id"] in live and live[run["id"]].active for run in runs):
            return  # nothing in view is changing
        sections = []
        for run in reversed(runs):  # oldest first reads as a story
            job = live.get(run["id"])
            lines = "\n".join(job.log) if job is not None else run["log"].rstrip("\n")
            heading = f"{run['kind']} — {_format_meeting_timestamp(run['started_at'])}"
            outcome, detail = _run_outcome(run, job)
            footer = f"{outcome}{': ' + detail if detail else ''}"
            sections.append(f"{heading}\n{lines or '(nothing reported)'}\n→ {footer}")
        if not sections:
            meeting_id = self._meeting_items.get(item)
            meeting = self.app.db.get_meeting(meeting_id) if meeting_id is not None else None
            sections.append(
                "No transcriptions recorded for this meeting — it was transcribed before this log existed."
                if meeting is not None and meeting.ended_at is not None
                else "No transcriptions recorded for this meeting yet."
            )
        at_end = self.log_text.yview()[1] >= 0.999
        _set_readonly_text(self.log_text, "\n\n".join(sections))
        if at_end or force:
            self.log_text.see("end")

    def _open_selected(self) -> None:
        meeting_id = self._selected_meeting_id()
        if meeting_id is not None:
            self.app.open_in_library(meeting_id)


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
            "of memory on medium/large. A size not used before is downloaded on first use. A \".en\" size only "
            "does English, a little more accurately than the one before it.",
        )
        self.language_var = tk.StringVar(value=_language_label(settings.transcription_language))
        self._field(body, 2, "Language", ttk.Combobox(
            body, textvariable=self.language_var, values=[name for _code, name in TRANSCRIPTION_LANGUAGES],
            state="readonly", width=20,
        ))
        self.vocabulary_var = tk.StringVar(value=", ".join(settings.custom_vocabulary))
        self._field(body, 3, "Your words", ttk.Entry(body, textvariable=self.vocabulary_var))
        self._hint(
            body, 4,
            "Names and terms to spell the way you type them, separated by commas — clients, products, "
            "acronyms. Each meeting's project, title, attendees, the names on Teams' captions and what you "
            "type in its notes are added on their own.",
        )
        self.read_outlook_var = tk.BooleanVar(value=settings.read_outlook_calendar)
        ttk.Checkbutton(
            body, text="Add the names on the meeting's Outlook invite", variable=self.read_outlook_var,
            style="Card.TCheckbutton",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._hint(
            body, 6,
            "Reads the invite from Outlook on this PC when the meeting is transcribed — nothing leaves the "
            "computer. Outlook must be open, and may ask once whether to allow it.",
        )
        # Applied as soon as they're ticked, not on Save: they decide what happens to the meeting being
        # recorded when it stops, and an unsaved untick used to leave it going to the cloud anyway.
        self.transcribe_locally_var = tk.BooleanVar(value=settings.transcribe_locally)
        self.transcribe_in_cloud_var = tk.BooleanVar(value=settings.transcribe_in_cloud)
        ttk.Checkbutton(
            body, text="Transcribe on this PC", variable=self.transcribe_locally_var,
            style="Card.TCheckbutton", command=self._on_transcription_choice,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Checkbutton(
            body, text="Transcribe in the cloud with Runpod — names the other side's speakers, and sends the "
            "meeting's audio (both sides) to the cloud",
            variable=self.transcribe_in_cloud_var, style="Card.TCheckbutton", command=self._on_transcription_choice,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._hint(
            body, 9,
            "At least one has to stay ticked. Tick both to keep both transcripts and compare them in the Library, "
            "where either can also be made later from a meeting's recording. Takes effect straight away, "
            "including for a meeting being recorded now.",
        )
        self._runpod = ttk.Frame(body, style="Card.TFrame", borderwidth=0)
        self._runpod.grid(row=10, column=0, columnspan=2, sticky="we")
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
        self.compress_recordings_var = tk.BooleanVar(value=settings.compress_recordings)
        ttk.Checkbutton(
            body, text="Compress recordings once transcribed", variable=self.compress_recordings_var,
            style="Card.TCheckbutton",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self._hint(
            body, 3,
            "Keeps each meeting's audio at about 14 MB an hour per track instead of 0.7 GB — plenty for "
            "listening back or transcribing again. Untick to keep the original WAV files.",
        )
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
            transcription_language=_language_code(self.language_var.get()),
            custom_vocabulary=self.vocabulary_var.get(),
            compress_recordings=self.compress_recordings_var.get(),
            read_outlook_calendar=self.read_outlook_var.get(),
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
