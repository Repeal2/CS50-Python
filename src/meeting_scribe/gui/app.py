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
from typing import Callable

from meeting_scribe.config import (
    WHISPER_MODEL_SIZES,
    Settings,
    load_settings,
    update_audio_automation,
    update_audio_devices,
    update_copilot_settings,
    update_diarize_system_audio,
    update_meeting_hotkeys,
    update_runpod_settings,
    update_whisper_model_size,
)
from meeting_scribe.audio.device_watch import device_signature
from meeting_scribe.gui.meeting_prompt import PromptCard, start_recording_prompt, stop_recording_prompt
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
from meeting_scribe.screen.region_picker import (
    RegionOutline,
    RegionTarget,
    WindowRegionTarget,
    pick_region_interactively,
    pin_region_to_window,
)
from meeting_scribe.screen.window_picker import WindowTarget, window_exists
from meeting_scribe.session import MeetingSession, retry_meeting_transcription
from meeting_scribe.storage.database import Database
from meeting_scribe.storage.documents import UnsupportedDocumentError, extract_text, save_original_copy
from meeting_scribe.transcription.engine import TranscriptLine, merge_transcript_lines, render_transcript

# Shown in a device dropdown in place of a device name, meaning "follow whatever Windows currently
# considers the default" rather than a specific device — used on both the Record tab and Settings tab.
SYSTEM_DEFAULT_LABEL = "System default"
AUTO_HEADSET_LABEL = "Recognize by name"

# Used when Start is clicked (or a Start hotkey fires — see MeetingScribeApp._handle_start_hotkey) with
# the project field left blank, so a meeting never fails to start for lack of a project name. The
# project field stays editable for the meeting's whole life (see RecordTab._on_project_field_committed),
# so recording under this placeholder is never a dead end — it can be moved to a real project any time
# before the meeting ends, the same way its title can be renamed from "Untitled meeting".
DEFAULT_PROJECT_NAME = "Unfiled"

# The title field's starting value — also what marks it as "not customized yet" for Start's Teams-name
# autofill (see RecordTab._detect_meeting_title): a title still exactly this gets a chance to be replaced
# by a detected Teams meeting name, same as a blank one would.
DEFAULT_MEETING_TITLE = "Untitled meeting"

# Manual-notes editor: matches leading whitespace plus an optional bullet marker ("-", "*", or "•")
# followed by a space, so Enter can continue the same bullet/indent on the next line. A plain line (no
# bullet) just matches its leading whitespace, so it stays plain rather than getting a bullet forced on.
_NOTES_BULLET_PREFIX = re.compile(r"^[ \t]*(?:[-*•]\s+)?")
_NOTES_INDENT = "    "

# Shown on the Projects tab for a meeting whose recording finished but whose transcription didn't (see
# session.retry_meeting_transcription) — e.g. a `mkl_malloc: failed to allocate memory` crash partway
# through a long meeting. The recorded audio survives that crash; only the transcript is missing.
_UNFINISHED_MEETING_NOTICE = (
    "Transcription didn't finish for this meeting, but the recording is safe. Retry to transcribe it "
    "— note that on-screen text captured during the meeting can't be recovered this way, only spoken "
    "audio."
)


_NO_RUNPOD_TRANSCRIPT_NOTICE = (
    "(No Runpod transcript for this meeting — either \"Identify speakers\" was off in Settings when it "
    "started, or Runpod failed; the activity log says which.)"
)


def _meeting_transcripts(rows) -> tuple[str, str, str]:
    """(on-screen text, audio transcribed on this PC, audio with Runpod's system track) as rendered text
    for the Projects tab, from a meeting's saved transcript_segments rows. Both audio versions include
    the same mic lines — only the system track differs — so each reads as the whole conversation. The
    Runpod one is "" when there are no Runpod lines. A line saved before transcripts were tagged by
    engine has none, and counts as this PC's."""
    screen, mic, local_system, runpod_system = [], [], [], []
    for row in rows:
        line = TranscriptLine(row["timestamp_seconds"], row["source"], row["text"], speaker=row["speaker"])
        if line.source == "screen_ocr":
            screen.append(line)
        elif line.source == "mic":
            mic.append(line)
        elif row["engine"] == "runpod":
            runpod_system.append(line)
        else:
            local_system.append(line)
    runpod_text = render_transcript(merge_transcript_lines(mic, runpod_system)) if runpod_system else ""
    return (
        render_transcript(screen),
        render_transcript(merge_transcript_lines(mic, local_system)),
        runpod_text,
    )


def _format_meeting_timestamp(iso_string: str) -> str:
    """Renders a stored UTC ISO timestamp (e.g. "2026-07-25T12:17:32.123456+00:00") in the user's local
    time and a human-readable format, instead of the raw string."""
    try:
        return datetime.fromisoformat(iso_string).astimezone().strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return iso_string


def _has_no_mic_signal(problems: "tuple[str, ...]") -> bool:
    """Whether `problems` (as returned by MeetingSession.input_problems()) currently includes the mic
    reading digital silence — used to flash the OCR region outline red (see
    RecordTab._refresh_input_warnings / screen.region_picker.RegionOutline.set_alert). Matches the exact
    wording audio.recorder._describe_stream_problem uses for that case ("no signal at all ... digital
    silence" — see its own "digital silence" test) rather than re-deriving the condition from scratch, so
    this stays in lockstep with whatever counts as that warning there."""
    return any(problem.startswith("Microphone:") and "digital silence" in problem for problem in problems)


def _device_choices(
    device_names: "list[str]", selected: str, default_label: str = SYSTEM_DEFAULT_LABEL
) -> "list[str]":
    """Dropdown values for a mic/system-audio picker: "System default" plus every device currently
    present — and the currently selected device even if it isn't present right now (unplugged). Keeping
    it rather than resetting the selection to "System default" matters now that the lists refresh on
    their own whenever a device comes or goes: a reset would silently become the saved setting on the
    next Settings save, and the choice would be lost for when the device is plugged back in (the
    recorder already falls back to the default, with a notice, while it's missing)."""
    values = [default_label] + [name for name in device_names if name != default_label]
    if selected and selected not in values:
        values.append(selected)
    return values


def _hotkey_label(combo: HotkeyCombo | None) -> str:
    return combo.label if combo is not None else "Not set"


def _enumerate_devices() -> tuple[list, list]:
    """A fresh (input devices, loopback devices) enumeration — current only while no meeting is
    recording (see MeetingScribeApp.refresh_device_lists). Empty lists off Windows (e.g. a dev machine)."""
    from meeting_scribe.audio.device_picker import list_input_devices, list_loopback_devices

    try:
        return list_input_devices(), list_loopback_devices()
    except RuntimeError:
        return [], []


def _hwnd_to_watch_for_auto_stop(
    target: "WindowTarget | RegionTarget | WindowRegionTarget | None",
) -> int | None:
    """The window handle "Stop when the screen-source window closes" should watch for this screen
    target, or None if there isn't one to watch. A plain RegionTarget (fixed screen coordinates) and
    "Entire screen" (None) have no associated window, so the checkbox simply has no effect when either is
    selected — there's nothing for it to notice closing."""
    if isinstance(target, (WindowTarget, WindowRegionTarget)):
        return target.hwnd
    return None


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
    # How often the mic/system-audio dropdowns check for devices being connected or disconnected.
    _DEVICE_POLL_MS = 2000
    # How often to check whether a Teams call has started (see _poll_teams_meeting).
    _MEETING_POLL_MS = 2000

    def __init__(self, settings: Settings | None = None):
        _enable_per_monitor_dpi_awareness()
        super().__init__()
        self.title("Meeting Scribe")
        self.geometry("980x680")

        self.settings = settings or load_settings()
        self.db = Database(self.settings.db_path)
        self._session: MeetingSession | None = None
        # Finish-up jobs (a stopped meeting's transcription, a Projects-tab retry) still running on
        # background threads — see run_in_background and _on_close.
        self._background_threads: list[threading.Thread] = []
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

        self._hotkey_listener: GlobalHotkeyListener | None = None
        self.apply_hotkeys()

        # State for _poll_devices: the last device signature seen while idle, and the last recorder
        # device-list version seen while recording (paired with the session it came from).
        self._idle_device_signature = device_signature()
        self._seen_device_list: tuple[MeetingSession | None, int] = (None, 0)
        self.after(self._DEVICE_POLL_MS, self._poll_devices)

        self._meeting_prompt_tracker = MeetingPromptTracker()
        self._meeting_end_tracker = MeetingEndTracker()
        self._start_prompt: PromptCard | None = None
        self._stop_prompt: PromptCard | None = None
        self._detected_meeting: DetectedMeeting | None = None
        # The work area of the monitor the call was last seen on — where the "meeting ended" prompt goes,
        # since by then the call window it would otherwise sit under has closed.
        self._meeting_work_area: dict | None = None
        if sys.platform == "win32":
            self.after(self._MEETING_POLL_MS, self._poll_teams_meeting)

    def apply_hotkeys(self) -> None:
        """(Re)starts the global-hotkey listener from self.settings' current start/stop combos — called
        once at startup and again from the Settings tab's Save button whenever either combo changes.
        Tearing down and rebuilding the whole listener rather than patching a live one is simplest and
        cheap, since this only ever runs right after startup or an explicit Save, never in a hot path."""
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            self._hotkey_listener = None

        bindings = {}
        if self.settings.start_meeting_hotkey is not None:
            bindings[1] = (self.settings.start_meeting_hotkey, lambda: self.after(0, self._handle_start_hotkey))
        if self.settings.stop_meeting_hotkey is not None:
            bindings[2] = (self.settings.stop_meeting_hotkey, lambda: self.after(0, self._record_tab._stop))
        if not bindings:
            return

        listener = GlobalHotkeyListener(bindings)
        try:
            failed = listener.start()
        except RuntimeError:
            return  # not on Windows (e.g. a dev machine) — hotkeys just aren't available there
        self._hotkey_listener = listener
        if failed:
            labels = ", ".join(combo.label for combo in failed)
            messagebox.showwarning(
                "Meeting Scribe",
                "Couldn't register this shortcut — it's likely already used by another application: "
                f"{labels}",
            )

    def _handle_start_hotkey(self) -> None:
        # The Start button is already disabled while a meeting is recording, which is what normally
        # prevents this — but a global hotkey bypasses button state entirely, so it needs its own guard
        # against starting a second meeting on top of one already running.
        if self._session is None:
            self._record_tab._start()

    def refresh_device_lists(self) -> None:
        """Repopulates the mic/system-audio dropdowns on both tabs with the current device list. While a
        meeting records, that list has to come from its recorder — a fresh enumeration would only see
        the snapshot PortAudio took when the meeting started (see audio.device_watch)."""
        session = self._session
        if session is not None:
            inputs, loopbacks = session.available_devices()
        else:
            inputs, loopbacks = _enumerate_devices()
        self._record_tab.apply_device_lists(inputs, loopbacks)
        self._settings_tab.apply_device_lists(inputs, loopbacks)

    def request_device_refresh(self) -> None:
        """The "Refresh devices" buttons. Idle, that's just a fresh enumeration. While recording, the
        recorder has to restart its audio to see anything new (see Recorder.reload_devices), which
        blocks briefly — so that runs in the background, and _poll_devices updates the dropdowns once
        the recorder's device list version changes."""
        session = self._session
        if session is None:
            self.refresh_device_lists()
            return

        def worker() -> None:
            try:
                session.reload_devices()
            except Exception as exc:
                self.after(0, self._record_tab._log, f"[{session.title}] Couldn't refresh devices: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _poll_devices(self) -> None:
        """Keeps the device dropdowns current as devices come and go, without anyone pressing Refresh.
        Recording: the recorder's own watcher reloads its device list on a change, and this just
        notices the version bump. Idle: a cheap winmm signature (audio.device_watch) says whether a
        fresh enumeration is worth doing at all."""
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

    def _poll_teams_meeting(self) -> None:
        """Checks for a Teams call on a background thread (it enumerates every window, too slow to do on
        the GUI thread every couple of seconds), then hands the result back here. The next check is only
        scheduled once this one has reported, so checks never pile up."""

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

            window_rect = None
            if meeting is not None:
                window_rect = self._measure_meeting_window(meeting)

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
        """The call window's current bounds (None if minimized), remembering which monitor it's on for
        the "meeting ended" prompt — see _meeting_work_area."""
        from meeting_scribe.screen.window_picker import get_monitor_work_area, get_window_region

        hwnd = meeting.window.hwnd if meeting.window is not None else None
        try:
            window_rect = get_window_region(hwnd) if hwnd is not None else None
            self._meeting_work_area = get_monitor_work_area(hwnd)
        except Exception:
            return None  # the call window closed since the check ran; the next check sorts it out
        return window_rect

    def _primary_work_area(self) -> dict | None:
        from meeting_scribe.screen.window_picker import get_monitor_work_area

        try:
            return get_monitor_work_area(None)
        except Exception:
            return None

    def close_meeting_prompts(self) -> None:
        """Called when recording starts or stops by any route, so a prompt offering to do what just
        happened doesn't linger until the next check."""
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
        # Uses the name detection already found rather than letting _start look it up again.
        if meeting is not None and meeting.name:
            if self._record_tab.title_var.get().strip() in ("", DEFAULT_MEETING_TITLE):
                self._record_tab.title_var.set(meeting.name)
        self._record_tab._start()

    def _stop_from_meeting_prompt(self) -> None:
        self._dismiss_stop_prompt()
        if self._session is not None:
            self._record_tab._stop()

    def refresh_project_lists(self) -> None:
        self._record_tab.refresh_projects()
        self._projects_tab.refresh_projects()

    def sync_device_displays(self) -> None:
        """The mic/system audio choice can be changed from either the Record tab or the Settings tab —
        whichever one didn't make the change calls this so it doesn't keep showing a stale value."""
        self._record_tab.sync_from_settings()
        self._settings_tab.sync_from_settings()

    def switch_active_recording(
        self,
        *,
        mic_changed: bool = False,
        mic_name: str | None = None,
        system_changed: bool = False,
        system_name: str | None = None,
    ) -> None:
        """If a meeting is actively recording right now, moves it onto the newly chosen device(s)
        immediately instead of leaving the recording on whatever device was open when it started — the
        old behavior, which silently kept recording an unused device for the rest of the meeting. Called
        by both the Record tab's live dropdowns and the Settings tab's Save button whenever either
        device actually changed. No-ops if nothing is currently recording, or if neither changed.

        Runs the switch on a background thread — it can block briefly joining the old capture thread —
        and reports the outcome to the Record tab's activity log once it's done, the same pattern
        _stop() uses for its own background finish-up work."""
        session = self._session
        if session is None or not (mic_changed or system_changed):
            return

        def worker() -> None:
            if mic_changed:
                self._switch_one_device(session.switch_mic_device, mic_name, "Microphone")
            if system_changed:
                self._switch_one_device(session.switch_system_device, system_name, "System audio")

        threading.Thread(target=worker, daemon=True).start()

    def _switch_one_device(
        self, switch: Callable[[str | None], None], device_name: str | None, label: str
    ) -> None:
        try:
            switch(device_name)
        except Exception as exc:  # surfaced to the user regardless of cause
            self.after(0, self._on_device_switch_failed, label, exc)
        else:
            self.after(0, self._on_device_switch_done, label, device_name)

    def _on_device_switch_done(self, label: str, device_name: str | None) -> None:
        shown = device_name or "System default"
        self._record_tab._log(f"{label} switched to {shown!r} mid-meeting.")

    def _on_device_switch_failed(self, label: str, exc: Exception) -> None:
        self._record_tab._log(f"{label} switch failed: {exc}")
        messagebox.showerror("Meeting Scribe", f"Couldn't switch {label.lower()}: {exc}")

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

    def run_in_background(self, target: Callable[[], None]) -> None:
        """Starts a meeting's finish-up work (transcription, pushing, saving) on a daemon thread, and
        remembers it so closing the window while it's still running can warn first rather than cut it
        off mid-transcript — see _on_close."""
        self._background_threads = [thread for thread in self._background_threads if thread.is_alive()]
        thread = threading.Thread(target=target, daemon=True)
        self._background_threads.append(thread)
        thread.start()

    def _on_close(self) -> None:
        """Closing used to just destroy the window, whatever was going on: a meeting still recording kept
        its capture threads reading PortAudio and writing WAV files while the interpreter shut down
        around them (daemon threads are killed mid-native-call at exit — a crash on the way out, and
        WAV files left without their final header), and a finishing meeting lost its transcript. Now it
        asks first, and a recording is stopped cleanly — its audio kept, so the meeting can be
        transcribed later with Retry on the Projects tab."""
        session = self._session
        finishing = any(thread.is_alive() for thread in self._background_threads)
        if session is not None or finishing:
            if session is not None:
                message = (
                    f'"{session.title}" is still recording. Close anyway? Recording will stop and the audio '
                    "will be kept — you can transcribe it later with Retry on the Projects tab."
                )
            else:
                message = (
                    "A meeting is still being transcribed/saved in the background. Close anyway? It will "
                    "be left unfinished — you can transcribe it later with Retry on the Projects tab."
                )
            if not messagebox.askyesno("Meeting Scribe", message, icon="warning", parent=self):
                return
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
        if session is not None:
            self._record_tab.flush_manual_notes()
            self._session = None
            try:
                session.abandon()
            except Exception:  # closing must still go ahead; the audio on disk is what matters
                self._report_callback_exception(*sys.exc_info())
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
    # Long enough to say a sentence into the microphone, short enough that nobody skips the test.
    MIC_TEST_SECONDS = 3.0
    # How often _poll_auto_stop checks whether a watched screen-source window has closed. Frequent enough
    # that "forgot to click Stop" doesn't leave a meeting recording for long after the call actually
    # ended; infrequent enough that it's not worth its own tighter polling loop than that.
    _AUTO_STOP_POLL_MS = 2000

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._window_targets: list = []
        self._region_target: RegionTarget | WindowRegionTarget | None = None
        self._region_outline: RegionOutline | None = None
        # The (session, hwnd) pair _poll_auto_stop watches for "stop when the screen-source window
        # closes" — None whenever that isn't in effect for whatever's currently recording (checkbox left
        # unchecked, or "Entire screen"/a fixed area was the screen source). Captured once at Start, like
        # screen_target itself; comparing the session by identity is what makes this automatically inert
        # the instant Stop is pressed (by the poll itself or manually) or a different meeting starts,
        # with no separate cleanup needed beyond what _stop() already does.
        self._auto_stop_watch: tuple[MeetingSession, int] | None = None
        self._input_devices: list = []
        self._loopback_devices: list = []
        # Which session's recorder notices have already been written to the activity log, how many of
        # them, and which live input problems have been mentioned — see _refresh_input_warnings.
        self._notice_session = None
        self._notices_logged = 0
        self._logged_input_problems: set[str] = set()

        form = ttk.Frame(self)
        form.pack(fill="x", padx=12, pady=12)

        ttk.Label(form, text="Project").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(form, textvariable=self.project_var, width=40)
        self.project_combo.grid(row=0, column=1, sticky="we", padx=6, pady=4)
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_field_committed)
        self.project_combo.bind("<FocusOut>", self._on_project_field_committed)
        self.project_combo.bind("<Return>", self._on_project_field_committed)
        ttk.Button(form, text="Add Project", command=self.app.prompt_new_project).grid(
            row=0, column=2, padx=(6, 0)
        )

        ttk.Label(form, text="Meeting title").grid(row=1, column=0, sticky="w")
        self.title_var = tk.StringVar(value=DEFAULT_MEETING_TITLE)
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

        # Off by default, and only meaningful when the screen source picked at Start is a specific
        # window or an area pinned to one (see _hwnd_to_watch_for_auto_stop) — there's no window handle
        # to watch for "Entire screen" or a fixed-position area, so checking this with either of those
        # selected simply has no effect. Read once at Start, like screen_target itself; changing the
        # dropdown or this checkbox mid-meeting doesn't retroactively change what's being watched.
        ttk.Label(form, text="Auto-stop").grid(row=4, column=0, sticky="w")
        self.stop_on_window_close_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form,
            text="Stop recording when the screen-source window closes",
            variable=self.stop_on_window_close_var,
        ).grid(row=4, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(form, text="Microphone").grid(row=5, column=0, sticky="w")
        self.mic_var = tk.StringVar(value=self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.mic_combo = ttk.Combobox(form, textvariable=self.mic_var, width=40, state="readonly")
        self.mic_combo.grid(row=5, column=1, sticky="we", padx=6, pady=4)
        self.mic_combo.bind("<<ComboboxSelected>>", self._on_devices_changed)

        ttk.Label(form, text="System audio (speaker)").grid(row=6, column=0, sticky="w")
        self.system_var = tk.StringVar(
            value=self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL
        )
        self.system_combo = ttk.Combobox(form, textvariable=self.system_var, width=40, state="readonly")
        self.system_combo.grid(row=6, column=1, sticky="we", padx=6, pady=4)
        self.system_combo.bind("<<ComboboxSelected>>", self._on_devices_changed)

        device_buttons = ttk.Frame(form)
        device_buttons.grid(row=5, column=2, rowspan=2, padx=(6, 0))
        ttk.Button(device_buttons, text="Refresh devices", command=self.app.request_device_refresh).pack(
            fill="x"
        )
        # Before a meeting is the only moment when a quiet microphone is unambiguous — the user knows
        # they're supposed to be making noise — and the moment when fixing it costs nothing.
        self.test_mic_button = ttk.Button(
            device_buttons, text="Test mic…", command=self._test_microphone
        )
        self.test_mic_button.pack(fill="x", pady=(4, 0))
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

        # A meter can't distinguish "muted" from "nobody is talking" — this is where the app says which
        # it thinks it is. Blank whenever both inputs look healthy, so anything showing here is worth
        # reading. Deliberately inline rather than a dialog: a modal popping up mid-meeting would steal
        # focus from the call it's warning about.
        self.input_warning_var = tk.StringVar(value="")
        ttk.Label(
            self,
            textvariable=self.input_warning_var,
            foreground="#b00020",
            wraplength=900,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(4, 0))

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
        self._poll_auto_stop()

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

    def _on_project_field_committed(self, _event=None) -> None:
        """Fires when the project field's value is "committed" — tabbed/clicked away from, Enter
        pressed, or an existing project picked from the dropdown — rather than on every keystroke the
        way the title field is. Committing a project change calls get_or_create_project, which would
        otherwise leave a throwaway row behind for every partially-typed character if it ran that
        eagerly.

        Refreshes the title-suggestions dropdown for whatever project is now entered (existing
        behavior), and — if a meeting is actively recording under a *different* project name — moves it
        to this one, the project-field counterpart to _on_title_changed."""
        self._refresh_title_suggestions()
        session = self.app._session
        if session is None:
            return
        project_name = self.project_var.get().strip()
        if not project_name or project_name == session.project.name:
            return
        session.set_project(project_name)
        self._log(f'[{session.title}] Moved to project "{project_name}".')

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
        self._refresh_input_warnings(session)
        self.after(150, self._poll_audio_levels)

    def _refresh_input_warnings(self, session) -> None:
        """Surfaces anything wrong with what's being captured, while the meeting can still be fixed.

        The level meters alone can't tell this story: a microphone whose capture thread has fallen over,
        or that was never the right device, reads exactly like a microphone nobody is talking into.
        Recorder notices (a dead capture thread, a substituted device) are statements of fact and go
        straight to the activity log, once each. Input problems (silence, clipping) are a live judgement
        that can correct itself, so they hold the warning line for as long as they last and are logged
        the first time each appears — noticing now beats finding out when the transcript comes back with
        one side of the conversation missing.

        A dead-silent mic additionally flashes the OCR region outline red (see _sync_alert_outline) —
        right at the thing a user is actually looking at during a meeting, not just another line in an
        activity log they may not have scrolled to."""
        if session is not self._notice_session:
            self._notice_session = session
            self._notices_logged = 0
            self._logged_input_problems = set()
        if session is None:
            self.input_warning_var.set("")
            self._sync_alert_outline(no_mic_signal=False)
            return

        notices = session.capture_notices()
        for message in notices[self._notices_logged:]:
            self._log(f"[{session.title}] {message}")
        self._notices_logged = len(notices)

        problems = session.input_problems()
        self.input_warning_var.set("\n".join(problems))
        for problem in problems:
            if problem not in self._logged_input_problems:
                self._logged_input_problems.add(problem)
                self._log(f"[{session.title}] {problem}")

        self._sync_alert_outline(no_mic_signal=_has_no_mic_signal(problems))

    def _sync_alert_outline(self, *, no_mic_signal: bool) -> None:
        if self._region_outline is not None:
            self._region_outline.set_alert(no_mic_signal)

    def _test_microphone(self) -> None:
        """Listens to the selected microphone for a few seconds and reports what it actually heard.

        Runs off the GUI thread — it's a few seconds of blocking stream reads — and works whether or not
        a meeting is recording, since WASAPI shared mode lets the same device be opened twice and "has
        my mic gone dead?" is exactly the question you want to answer mid-meeting."""
        from meeting_scribe.audio.recorder import check_input_device

        device_name = None if self.mic_var.get() == SYSTEM_DEFAULT_LABEL else self.mic_var.get()
        self.test_mic_button["state"] = "disabled"
        self.test_mic_button["text"] = "Listening…"
        self._log(f"Mic test: listening for {self.MIC_TEST_SECONDS:.0f}s — say something.")

        def worker() -> None:
            try:
                result = check_input_device(device_name, seconds=self.MIC_TEST_SECONDS)
            except Exception as exc:  # surfaced to the user regardless of cause
                self.after(0, self._on_mic_test_failed, exc)
                return
            self.after(0, self._on_mic_test_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_mic_test_done(self, result) -> None:
        self._reset_test_mic_button()
        self._log(f"Mic test: {result.summary}")
        show = messagebox.showinfo if result.is_ok else messagebox.showwarning
        show("Meeting Scribe", result.summary)

    def _on_mic_test_failed(self, exc: Exception) -> None:
        self._reset_test_mic_button()
        self._log(f"Mic test failed: {exc}")
        messagebox.showerror("Meeting Scribe", f"Couldn't test that microphone: {exc}")

    def _reset_test_mic_button(self) -> None:
        self.test_mic_button["state"] = "normal"
        self.test_mic_button["text"] = "Test mic…"

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

    def _poll_auto_stop(self) -> None:
        """Stops the currently recording meeting on its own once its screen-source window (a specific
        window, or an area pinned to one) closes — for apps like Teams/Zoom that open a distinct call
        window and close it, not just minimize it, the instant the call ends, this means a forgotten Stop
        button doesn't leave the meeting recording indefinitely. Only in effect when "Stop when the
        screen-source window closes" was checked at Start (see _start's use of
        _hwnd_to_watch_for_auto_stop) — a no-op otherwise, including for "Entire screen" or a
        fixed-position area, neither of which has a window to watch."""
        if self._auto_stop_watch is not None:
            session, hwnd = self._auto_stop_watch
            if session is self.app._session and not window_exists(hwnd):
                self._auto_stop_watch = None
                self._log(f'[{session.title}] Screen-source window closed — stopping automatically.')
                self._stop()
        self.after(self._AUTO_STOP_POLL_MS, self._poll_auto_stop)

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

    def _detect_meeting_title(self) -> str | None:
        """Best-effort autofill for a still-default meeting title, from whichever Teams window looks
        like the active call — see window_picker.find_teams_meeting_name. Never blocks or errors the
        meeting from starting: a detection failure (Teams isn't running, isn't in a call, or this isn't
        Windows — e.g. a dev machine) just leaves the title at its default, exactly as if this didn't
        exist. Independent of the "Screen source" dropdown — Teams doesn't need to be the OCR target for
        its meeting name to be worth picking up."""
        from meeting_scribe.screen.window_picker import find_teams_meeting_name

        try:
            return find_teams_meeting_name()
        except Exception:  # not Windows, or a win32 call failing on some window — never worth a failed Start
            return None

    def _refresh_devices(self) -> None:
        """Fills the microphone/system-audio dropdowns at startup — the same picker as the Settings tab,
        surfaced here too so switching devices doesn't require leaving the Record tab. Later refreshes go
        through MeetingScribeApp.refresh_device_lists, which knows where a current list comes from."""
        self.apply_device_lists(*_enumerate_devices())

    def apply_device_lists(self, input_devices, loopback_devices) -> None:
        self._input_devices = input_devices
        self._loopback_devices = loopback_devices
        self.mic_combo["values"] = _device_choices([d.name for d in input_devices], self.mic_var.get())
        self.system_combo["values"] = _device_choices(
            [d.name for d in loopback_devices], self.system_var.get()
        )

    def _on_devices_changed(self, _event=None) -> None:
        """Persists the mic/system choice immediately (rather than waiting for a Settings-tab Save) so
        it takes effect the next time the user hits Start Meeting — and, if a meeting is actively
        recording right now, switches that recording onto the newly chosen device(s) immediately too
        (see MeetingScribeApp.switch_active_recording)."""
        mic_name = None if self.mic_var.get() == SYSTEM_DEFAULT_LABEL else self.mic_var.get()
        system_name = None if self.system_var.get() == SYSTEM_DEFAULT_LABEL else self.system_var.get()
        mic_changed = mic_name != self.app.settings.mic_device_name
        system_changed = system_name != self.app.settings.system_device_name
        self.app.settings = update_audio_devices(
            self.app.settings, mic_device_name=mic_name, system_device_name=system_name
        )
        self.app.sync_device_displays()
        self.app.switch_active_recording(
            mic_changed=mic_changed, mic_name=mic_name, system_changed=system_changed, system_name=system_name
        )

    def _log(self, message: str) -> None:
        """Appends a timestamped line to the activity log and scrolls to it. This is a log of what the
        app is doing (started, stopped, transcribing, waiting on notes...), not a transcript preview —
        the widget is otherwise kept disabled so it reads as a log rather than an editable text box.

        Also appended to a file next to the database: the Text widget alone only exists in memory, so a
        process that dies for any reason — including the kind of native crash that bypasses every
        try/except in this app, the one this line exists for — takes whatever it was logging right before
        that (a low-memory warning, a recorder notice, "Transcription complete.") down with it, leaving
        nothing to look at afterward. A file survives the crash even when the window doesn't."""
        timestamp = datetime.now().strftime("%b %d, %Y %I:%M:%S %p")
        line = f"[{timestamp}] {message}"
        self.output["state"] = "normal"
        self.output.insert("end", line + "\n")
        self.output["state"] = "disabled"
        self.output.see("end")
        try:
            with open(self.app.settings.data_dir / "activity.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # logging shouldn't itself be able to take the app down

    def _start(self) -> None:
        project_name = self.project_var.get().strip() or DEFAULT_PROJECT_NAME
        meeting_title = self.title_var.get().strip() or DEFAULT_MEETING_TITLE
        if meeting_title == DEFAULT_MEETING_TITLE:
            meeting_title = self._detect_meeting_title() or DEFAULT_MEETING_TITLE
        screen_target = self._selected_screen_target()
        try:
            session = MeetingSession(
                self.app.settings,
                self.app.db,
                project_name,
                meeting_title,
                screen_target=screen_target,
            )
            session.start()
        except Exception as exc:  # a missing/unavailable audio device raises OSError, not RuntimeError
            # Shown rather than left to report_callback_exception's log file: with only the log, a failed
            # Start — particularly from the global hotkey, with this window not even in front — looks
            # exactly like nothing happening at all.
            messagebox.showerror("Meeting Scribe", f"Couldn't start recording: {exc}")
            return

        self.app._session = session
        self.app.close_meeting_prompts()
        # Reflects the DEFAULT_PROJECT_NAME/detected-title fallbacks back into their fields, so what's
        # shown always matches what the meeting is actually recording under — same reasoning either way:
        # a placeholder that isn't reflected in the field it stands in for isn't actually saved anywhere.
        self.project_var.set(project_name)
        self.title_var.set(meeting_title)
        # Only takes effect if the screen source has a window to watch in the first place — see
        # _hwnd_to_watch_for_auto_stop — and only for this meeting: unchecking the box or changing the
        # screen source afterwards doesn't retroactively stop watching (or start watching) anything.
        watched_hwnd = _hwnd_to_watch_for_auto_stop(screen_target) if self.stop_on_window_close_var.get() else None
        self._auto_stop_watch = (session, watched_hwnd) if watched_hwnd is not None else None
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

    def flush_manual_notes(self) -> None:
        """Saves any not-yet-saved manual notes right now instead of waiting out the debounce timer."""
        if self._manual_notes_save_after_id is not None:
            self.after_cancel(self._manual_notes_save_after_id)
            self._manual_notes_save_after_id = None
        self._save_manual_notes()

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
        self.flush_manual_notes()

        # Detach right away rather than waiting for the background job below to finish — transcribing
        # and pushing to Copilot Studio takes a moment, and there's no reason that should block starting
        # the next meeting. Everything the background job needs (the session, its title) is already
        # captured in this closure, so the app has no more use for the "current" session slot.
        self.app._session = None
        self.app.close_meeting_prompts()
        # Redundant when _poll_auto_stop is what called _stop() (it already cleared this before doing
        # so), but necessary for a manual click on the Stop button — either way, nothing should still be
        # watching a session that's no longer the active one.
        self._auto_stop_watch = None

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

        self.app.run_in_background(worker)

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
        except Exception as exc:  # a corrupt/malformed file (bad PDF, bad DOCX, unreadable image,
            # Tesseract missing) shouldn't fail with no visible feedback at all — extract_text only
            # promises to raise UnsupportedDocumentError for a file type it doesn't recognize, not for
            # one it recognizes but can't actually parse.
            messagebox.showerror("Meeting Scribe", f"Couldn't read {path.name!r}: {exc}")
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

        # Shown only for a meeting whose recording finished but whose transcription didn't (e.g. it hit
        # an out-of-memory error) — the audio is safe on disk, so this re-runs transcription from it
        # instead of asking the user to re-record. Hidden (empty label, disabled button) for a meeting
        # that already has a transcript.
        retry_bar = ttk.Frame(right)
        retry_bar.pack(fill="x", pady=(0, 8))
        self.retry_status_var = tk.StringVar()
        ttk.Label(
            retry_bar, textvariable=self.retry_status_var, foreground="#a33", wraplength=440,
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        self.retry_button = ttk.Button(
            retry_bar, text="Retry Transcription…", command=self._retry_transcription, state="disabled"
        )
        self.retry_button.pack(side="right", anchor="n")

        # Selecting a meeting fans its details out across these tabs, rather than dumping everything
        # into one pane — the raw on-screen OCR log, the spoken-audio transcript, and the notes the user
        # typed themselves during the meeting are different things a user reaches for at different times.
        self.detail_notebook = ttk.Notebook(right)
        self.detail_notebook.pack(fill="both", expand=True)

        self.ocr_text = _add_scrollable_text_tab(self.detail_notebook, "OCR Transcript")
        # The system audio can be transcribed both on this PC and by Runpod (see
        # session._transcribe_system_track) — one tab each, both with the same mic lines, for comparing.
        self.audio_text = _add_scrollable_text_tab(self.detail_notebook, "Audio (this PC)")
        self.runpod_audio_text = _add_scrollable_text_tab(self.detail_notebook, "Audio (Runpod)")
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
        except Exception as exc:  # a corrupt/malformed file (bad PDF, bad DOCX, unreadable image,
            # Tesseract missing) shouldn't fail with no visible feedback at all — extract_text only
            # promises to raise UnsupportedDocumentError for a file type it doesn't recognize, not for
            # one it recognizes but can't actually parse.
            messagebox.showerror("Meeting Scribe", f"Couldn't read {path.name!r}: {exc}")
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
        for widget in (
            self.ocr_text, self.audio_text, self.runpod_audio_text, self.manual_notes_text, self.attendees_text
        ):
            _set_text(widget, "")
        self._meeting_documents = []
        self.meeting_documents_list.delete(0, "end")
        _set_text(self.meeting_document_viewer, "")
        self.retry_status_var.set("")
        self.retry_button["state"] = "disabled"

    def _on_meeting_selected(self, _event) -> None:
        meeting = self._selected_meeting()
        if meeting is None:
            return

        ocr_text, local_text, runpod_text = _meeting_transcripts(self.app.db.get_segments(meeting.id))
        _set_text(self.ocr_text, ocr_text or "(no on-screen text captured)")
        _set_text(self.audio_text, local_text or "(no speech captured)")
        _set_text(self.runpod_audio_text, runpod_text or _NO_RUNPOD_TRANSCRIPT_NOTICE)
        _set_text(self.manual_notes_text, meeting.manual_notes or "(no manual notes for this meeting)")
        _set_text(self.attendees_text, meeting.attendees or "(no attendees captured)")

        if meeting.ended_at is None:
            self.retry_status_var.set(_UNFINISHED_MEETING_NOTICE)
            self.retry_button["state"] = "normal"
        else:
            self.retry_status_var.set("")
            self.retry_button["state"] = "disabled"

        self._load_meeting_documents(meeting.id)

    def _retry_transcription(self) -> None:
        """Re-runs transcription for the selected (unfinished) meeting on a background thread — mirrors
        RecordTab._stop's pattern of keeping the UI responsive while faster-whisper runs. Disabled while
        it's running so a slow retry can't be double-clicked into two overlapping ones for the same
        meeting."""
        meeting = self._selected_meeting()
        if meeting is None:
            return
        self.retry_button["state"] = "disabled"
        self.retry_status_var.set(f'Retrying transcription for "{meeting.title}"…')

        def report(message: str) -> None:
            # Surfaces things like transcription.engine's low-memory warning while the retry is running —
            # but only if the user is still looking at this meeting (see _still_viewing).
            self.after(0, self._show_retry_progress, meeting.id, message)

        def worker() -> None:
            try:
                retry_meeting_transcription(self.app.settings, self.app.db, meeting.id, on_progress=report)
            except Exception as exc:
                self.after(0, self._on_retry_failed, meeting.id, meeting.title, exc)
                return
            self.after(0, self._on_retry_done, meeting.id, meeting.title)

        self.app.run_in_background(worker)

    def _show_retry_progress(self, meeting_id: int, message: str) -> None:
        if self._still_viewing(meeting_id):
            self.retry_status_var.set(message)

    def _still_viewing(self, meeting_id: int) -> bool:
        """Whether the meeting a background retry just finished for is still the one on screen — by the
        time a retry completes, the user may have clicked to a different meeting or project entirely, and
        neither the selection nor the detail pane should jump back out from under them (same reasoning as
        RecordTab._on_stop_done not overwriting a newer status)."""
        selected = self._selected_meeting()
        return selected is not None and selected.id == meeting_id

    def _on_retry_done(self, meeting_id: int, meeting_title: str) -> None:
        still_viewing_it = self._still_viewing(meeting_id)
        if self._selected_project() is not None:
            self._on_project_selected(None)  # reloads the meeting list, now with this one finished
        if still_viewing_it:
            for index, meeting in enumerate(self._meetings):
                if meeting.id == meeting_id:
                    self.meeting_list.selection_set(index)
                    self.meeting_list.see(index)
                    self._on_meeting_selected(None)
                    break
        messagebox.showinfo("Meeting Scribe", f'"{meeting_title}" transcribed successfully.')

    def _on_retry_failed(self, meeting_id: int, meeting_title: str, exc: Exception) -> None:
        if self._still_viewing(meeting_id):
            self.retry_status_var.set(_UNFINISHED_MEETING_NOTICE)
            self.retry_button["state"] = "normal"
        messagebox.showerror("Meeting Scribe", f'Couldn\'t retry "{meeting_title}": {exc}')

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
    """Copilot push folder, audio device selection, transcription model size, and global meeting
    shortcuts — all editable without touching environment variables. Saved settings are written to disk
    (see config.save_user_config) and take effect immediately for this running session."""

    def __init__(self, parent: ttk.Notebook, app: MeetingScribeApp):
        super().__init__(parent)
        self.app = app
        self._input_devices: list = []
        self._loopback_devices: list = []
        # Staged, not applied until Save — mirrors every other field on this tab. None means "mouse
        # only" for that action. Which one (if either) is currently being (re)captured; see
        # _begin_hotkey_capture.
        self._pending_start_hotkey: HotkeyCombo | None = self.app.settings.start_meeting_hotkey
        self._pending_stop_hotkey: HotkeyCombo | None = self.app.settings.stop_meeting_hotkey
        self._capturing_hotkey: str | None = None

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

        ttk.Button(form, text="Refresh devices", command=self.app.request_device_refresh).grid(
            row=1, column=2, rowspan=2, padx=(6, 0)
        )

        # See config.Settings.auto_switch_audio_devices / headset_microphone_name.
        ttk.Label(form, text="Headset microphone").grid(row=3, column=0, sticky="w")
        self.headset_var = tk.StringVar(
            value=self.app.settings.headset_microphone_name or AUTO_HEADSET_LABEL
        )
        self.headset_combo = ttk.Combobox(form, textvariable=self.headset_var, width=45, state="readonly")
        self.headset_combo.grid(row=3, column=1, sticky="we", padx=6, pady=4)

        self.auto_switch_var = tk.BooleanVar(value=self.app.settings.auto_switch_audio_devices)
        ttk.Checkbutton(
            form,
            text="Switch devices automatically — follow the speaker that's playing; use the headset mic "
            "when it's live",
            variable=self.auto_switch_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Label(form, text="Transcription model").grid(row=5, column=0, sticky="w")
        self.whisper_model_var = tk.StringVar(value=self.app.settings.whisper_model_size)
        self.whisper_model_combo = ttk.Combobox(
            form,
            textvariable=self.whisper_model_var,
            values=WHISPER_MODEL_SIZES,
            width=45,
            state="readonly",
        )
        self.whisper_model_combo.grid(row=5, column=1, sticky="we", padx=6, pady=4)

        # Off by default — see config.Settings.diarize_system_audio. Only takes effect once the Runpod/
        # HuggingFace environment variables it needs are also set (see transcription.runpod_whisperx);
        # left unconfigured, opting in here just falls back to local transcription with a status message.
        self.diarize_system_audio_var = tk.BooleanVar(value=self.app.settings.diarize_system_audio)
        ttk.Checkbutton(
            form,
            text="Identify speakers in the system-audio track (sends audio to Runpod + HuggingFace)",
            variable=self.diarize_system_audio_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)

        # Credentials for the diarization endpoint above — plaintext in settings.json either way, so this
        # isn't meaningfully less secure than an environment variable would have been for a single-user
        # desktop app; it's just easier to set. runpod_huggingface_token can be left blank if HF_TOKEN was
        # instead set directly as an environment variable on the Runpod endpoint itself.
        ttk.Label(form, text="Runpod API key").grid(row=7, column=0, sticky="w")
        self.runpod_api_key_var = tk.StringVar(value=self.app.settings.runpod_api_key or "")
        ttk.Entry(form, textvariable=self.runpod_api_key_var, width=48, show="*").grid(
            row=7, column=1, sticky="we", padx=6, pady=4
        )

        ttk.Label(form, text="Runpod endpoint ID").grid(row=8, column=0, sticky="w")
        self.runpod_endpoint_id_var = tk.StringVar(value=self.app.settings.runpod_endpoint_id or "")
        ttk.Entry(form, textvariable=self.runpod_endpoint_id_var, width=48).grid(
            row=8, column=1, sticky="we", padx=6, pady=4
        )

        ttk.Label(form, text="HuggingFace token").grid(row=9, column=0, sticky="w")
        self.runpod_hf_token_var = tk.StringVar(value=self.app.settings.runpod_huggingface_token or "")
        ttk.Entry(form, textvariable=self.runpod_hf_token_var, width=48, show="*").grid(
            row=9, column=1, sticky="we", padx=6, pady=4
        )

        # Global (system-wide) shortcuts — work even while focused in Teams, not just this app. Off
        # ("Not set") until explicitly captured here; see hotkeys.py and MeetingScribeApp.apply_hotkeys.
        ttk.Label(form, text="Start meeting shortcut").grid(row=10, column=0, sticky="w")
        self.start_hotkey_var = tk.StringVar(value=_hotkey_label(self._pending_start_hotkey))
        ttk.Label(form, textvariable=self.start_hotkey_var).grid(row=10, column=1, sticky="w", padx=6, pady=4)
        start_hotkey_buttons = ttk.Frame(form)
        start_hotkey_buttons.grid(row=10, column=2, padx=(6, 0))
        self.start_hotkey_button = ttk.Button(
            start_hotkey_buttons, text="Change…", command=lambda: self._begin_hotkey_capture("start")
        )
        self.start_hotkey_button.pack(side="left")
        ttk.Button(start_hotkey_buttons, text="Clear", command=lambda: self._clear_hotkey("start")).pack(
            side="left", padx=(4, 0)
        )

        ttk.Label(form, text="Stop meeting shortcut").grid(row=11, column=0, sticky="w")
        self.stop_hotkey_var = tk.StringVar(value=_hotkey_label(self._pending_stop_hotkey))
        ttk.Label(form, textvariable=self.stop_hotkey_var).grid(row=11, column=1, sticky="w", padx=6, pady=4)
        stop_hotkey_buttons = ttk.Frame(form)
        stop_hotkey_buttons.grid(row=11, column=2, padx=(6, 0))
        self.stop_hotkey_button = ttk.Button(
            stop_hotkey_buttons, text="Change…", command=lambda: self._begin_hotkey_capture("stop")
        )
        self.stop_hotkey_button.pack(side="left")
        ttk.Button(stop_hotkey_buttons, text="Clear", command=lambda: self._clear_hotkey("stop")).pack(
            side="left", padx=(4, 0)
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
            "to confirm a device is actually picking up audio.\n\n"
            "Switch devices automatically (on by default) takes care of both during a meeting. System "
            "audio: once the recorded speaker has been quiet for a few seconds, the others are listened "
            "to and whichever is playing — only one plays the call — is recorded instead. Microphone: "
            "the headset microphone is used whenever it's connected and live, and the Microphone above "
            "(your laptop's) otherwise — including when the headset is muted or switched off. Pick your "
            "headset under Headset microphone, or leave it on \"Recognize by name\" to use any input "
            "Windows calls a headset. Every switch is written to the activity log, and picking a device "
            "by hand during a meeting turns the automation off for that meeting.\n\n"
            "Transcription model trades accuracy for memory/CPU: smaller (tiny/base/small) is faster and "
            "lighter but makes more mistakes, larger (medium/large) is more accurate but needs "
            "meaningfully more RAM — a long meeting can fail to transcribe with an out-of-memory error "
            "on a larger model where a smaller one would have finished fine. The first meeting after "
            "switching to a size that hasn't been used before downloads it, which needs internet access "
            "and can take a while for the larger sizes; only meetings started after Save pick up the "
            "change, so anything currently recording or still finishing up keeps using the old size.\n\n"
            "Identify speakers sends the system-audio track (everyone but you) to a cloud WhisperX "
            "endpoint on Runpod for transcription with speaker diarization, instead of transcribing it "
            "locally like everything else in this app — the tradeoff for telling who said what is that "
            "meeting audio leaves this machine. It needs a Runpod API key and endpoint ID (from your "
            "Runpod account's Serverless dashboard) entered below; a HuggingFace token is only needed "
            "here if one wasn't already set directly on the Runpod endpoint itself. Checking this box "
            "without finishing that setup just falls back to local transcription with a status message, "
            "not an error.\n\n"
            "Meeting shortcuts work system-wide — from inside Teams, not just this app — so a meeting can "
            "be started or stopped without switching windows first. Click Change…, then press the combo "
            "you want (needs at least one of Ctrl/Alt/Shift/Win); Esc cancels. Starting reuses whichever "
            "project and title are currently filled in on the Record tab, exactly like clicking Start "
            "Meeting there. Left \"Not set\", that action stays mouse-only. A shortcut already claimed by "
            "another application on this PC will fail to register — you'll see a warning naming which "
            "one, and it just won't fire until changed to something else."
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
        """Fills the mic/system-audio dropdowns at startup; see RecordTab._refresh_devices."""
        self.apply_device_lists(*_enumerate_devices())

    def apply_device_lists(self, input_devices, loopback_devices) -> None:
        self._input_devices = input_devices
        self._loopback_devices = loopback_devices
        self.mic_combo["values"] = _device_choices([d.name for d in input_devices], self.mic_var.get())
        self.system_combo["values"] = _device_choices(
            [d.name for d in loopback_devices], self.system_var.get()
        )
        self.headset_combo["values"] = _device_choices(
            [d.name for d in input_devices], self.headset_var.get(), default_label=AUTO_HEADSET_LABEL
        )

    def _begin_hotkey_capture(self, which: str) -> None:
        """Starts listening for the next real key combo pressed anywhere in the app, to assign as the
        Start or Stop meeting shortcut. Only listens in-app (bind_all, not a global hook) — capturing
        what to register is a one-off interaction with this tab, unlike triggering the shortcut itself
        afterward, which is exactly the point of registering it globally in the first place."""
        if self._capturing_hotkey is not None:
            return  # already capturing the other one; ignore a second click until that one finishes
        self._capturing_hotkey = which
        var = self.start_hotkey_var if which == "start" else self.stop_hotkey_var
        button = self.start_hotkey_button if which == "start" else self.stop_hotkey_button
        var.set("Press keys… (Esc to cancel)")
        button["state"] = "disabled"
        self.bind_all("<KeyPress>", self._on_hotkey_capture_keypress)

    def _on_hotkey_capture_keypress(self, event: "tk.Event") -> None:
        if event.keysym == "Escape":
            self._end_hotkey_capture(new_combo=None, cancelled=True)
            return
        if is_modifier_keysym(event.keysym):
            return  # just a modifier on its own so far; keep listening for the "main" key
        if key_name(event.keycode) is None:
            return  # not one of the keys this capture UI supports (letters/digits/F-keys); keep waiting
        modifiers = current_modifiers()
        if modifiers == 0:
            return  # a global shortcut needs at least one modifier held; keep waiting
        self._end_hotkey_capture(new_combo=HotkeyCombo(modifiers=modifiers, vk=event.keycode), cancelled=False)

    def _end_hotkey_capture(self, *, new_combo: HotkeyCombo | None, cancelled: bool) -> None:
        which, self._capturing_hotkey = self._capturing_hotkey, None
        self.unbind_all("<KeyPress>")
        if which is None:
            return
        if not cancelled:
            if which == "start":
                self._pending_start_hotkey = new_combo
            else:
                self._pending_stop_hotkey = new_combo
        var = self.start_hotkey_var if which == "start" else self.stop_hotkey_var
        button = self.start_hotkey_button if which == "start" else self.stop_hotkey_button
        combo = self._pending_start_hotkey if which == "start" else self._pending_stop_hotkey
        var.set(_hotkey_label(combo))
        button["state"] = "normal"

    def _clear_hotkey(self, which: str) -> None:
        if which == "start":
            self._pending_start_hotkey = None
            self.start_hotkey_var.set(_hotkey_label(None))
        else:
            self._pending_stop_hotkey = None
            self.stop_hotkey_var.set(_hotkey_label(None))

    def _save(self) -> None:
        if (
            self._pending_start_hotkey is not None
            and self._pending_start_hotkey == self._pending_stop_hotkey
        ):
            messagebox.showerror("Meeting Scribe", "Start and Stop shortcuts can't be the same combo.")
            return

        mic_name = None if self.mic_var.get() == SYSTEM_DEFAULT_LABEL else self.mic_var.get()
        system_name = (
            None if self.system_var.get() == SYSTEM_DEFAULT_LABEL else self.system_var.get()
        )
        mic_changed = mic_name != self.app.settings.mic_device_name
        system_changed = system_name != self.app.settings.system_device_name

        self.app.settings = update_copilot_settings(
            self.app.settings, copilot_sync_dir=self.sync_dir_var.get().strip()
        )
        self.app.settings = update_audio_devices(
            self.app.settings, mic_device_name=mic_name, system_device_name=system_name
        )
        self.app.settings = update_audio_automation(
            self.app.settings,
            auto_switch_audio_devices=self.auto_switch_var.get(),
            headset_microphone_name=(
                None if self.headset_var.get() == AUTO_HEADSET_LABEL else self.headset_var.get()
            ),
        )
        self.app.settings = update_whisper_model_size(
            self.app.settings, whisper_model_size=self.whisper_model_var.get()
        )
        self.app.settings = update_diarize_system_audio(
            self.app.settings, diarize_system_audio=self.diarize_system_audio_var.get()
        )
        self.app.settings = update_runpod_settings(
            self.app.settings,
            runpod_api_key=self.runpod_api_key_var.get().strip(),
            runpod_endpoint_id=self.runpod_endpoint_id_var.get().strip(),
            runpod_huggingface_token=self.runpod_hf_token_var.get().strip(),
        )
        self.app.settings = update_meeting_hotkeys(
            self.app.settings, start=self._pending_start_hotkey, stop=self._pending_stop_hotkey
        )
        self.app.apply_hotkeys()
        self._refresh_status()
        self.app.sync_device_displays()
        # If a meeting is actively recording, move it onto the newly chosen device(s) now rather than
        # only applying the change to the next meeting — see MeetingScribeApp.switch_active_recording.
        self.app.switch_active_recording(
            mic_changed=mic_changed, mic_name=mic_name, system_changed=system_changed, system_name=system_name
        )
        messagebox.showinfo("Meeting Scribe", "Settings saved.")

    def sync_from_settings(self) -> None:
        """Reflects a device change made elsewhere (e.g. the Record tab's own dropdowns) so this tab's
        dropdowns don't show a stale value for the same setting."""
        self.mic_var.set(self.app.settings.mic_device_name or SYSTEM_DEFAULT_LABEL)
        self.system_var.set(self.app.settings.system_device_name or SYSTEM_DEFAULT_LABEL)
