"""Orchestrates one meeting end to end: start recording (mic + system audio + screen), and on stop,
transcribe, merge into one transcript, push it to Copilot Studio, and file everything locally under the
meeting's project so there's a record of it in this app regardless of what happens to the pushed copy.

Each MeetingSession is self-contained (its own Recorder, ScreenWatcher, WhisperTranscriber, and meeting
directory) with no shared mutable state between instances, so one session's stop() — the slow part, given
local transcription — can safely keep running on a background thread while a new MeetingSession starts
and records the next meeting. The GUI relies on this for back-to-back meetings: starting the next one
doesn't wait for the previous one to finish transcribing/saving.
"""

from __future__ import annotations

from typing import Callable

from meeting_scribe.ai.copilot_push import format_meeting_for_push, push_meeting
from meeting_scribe.audio.recorder import Recorder
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent, ScreenWatcher
from meeting_scribe.screen.region_picker import RegionTarget
from meeting_scribe.screen.window_picker import WindowTarget
from meeting_scribe.storage.database import Database
from meeting_scribe.transcription.engine import (
    TranscriptLine,
    WhisperTranscriber,
    merge_transcript_lines,
    render_transcript,
)


class MeetingSession:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        project_name: str,
        title: str,
        screen_target: WindowTarget | RegionTarget | None = None,
    ):
        """`screen_target` selects a single window, or a fixed user-drawn rectangle, to OCR instead of
        the whole screen — e.g. just the Teams/Zoom window, or just a captions bar. Leave it None to
        capture the whole screen."""
        self._settings = settings
        self._db = db
        self.project = db.get_or_create_project(project_name)
        self.meeting_id = db.create_meeting(self.project.id, title)
        self.title = title

        meeting_dir = settings.meeting_dir(self.project.slug, self.meeting_id)
        meeting_dir.mkdir(parents=True, exist_ok=True)

        self._recorder = Recorder(
            meeting_dir,
            mic_device_name=settings.mic_device_name,
            system_device_name=settings.system_device_name,
        )
        self._screen_events: list[ScreenTextEvent] = []
        self._screen_watcher = ScreenWatcher(
            on_text=self._screen_events.append,
            interval_seconds=settings.screen_capture_interval_seconds,
            tesseract_cmd=settings.tesseract_cmd,
            target=screen_target,
        )
        self._transcriber = WhisperTranscriber(model_size=settings.whisper_model_size)

    def start(self) -> None:
        self._recorder.start()
        self._screen_watcher.start()

    def audio_levels(self) -> tuple[float, float]:
        """Current (mic, system) input levels, roughly 0..1 — lets the GUI show a live "is this actually
        picking up audio" meter while recording. Both are 0.0 before start() or after stop()."""
        return self._recorder.mic_level, self._recorder.system_level

    def stop(self, on_progress: Callable[[str], None] | None = None) -> str:
        """Stops recording, transcribes, pushes the meeting to Copilot Studio (if configured), and saves
        it locally. Returns the plain transcript — there's no AI-generated notes to return anymore; that
        happens downstream, outside this app, once Copilot Studio picks up the pushed file.

        `on_progress`, if given, is called with a short human-readable status at each stage, since this
        whole method can take a while (local transcription isn't instant) and the caller (the GUI) uses
        it to keep an activity log current instead of the UI looking frozen with no feedback."""

        def report(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        self._screen_watcher.stop()
        recorded = self._recorder.stop()
        report("Recording stopped.")

        mic_lines = self._transcriber.transcribe(recorded.mic_path, source="mic")
        system_lines = self._transcriber.transcribe(recorded.system_path, source="system")
        screen_lines = [
            TranscriptLine(event.timestamp_seconds, "screen_ocr", event.text)
            for event in self._screen_events
        ]
        report("Transcription complete.")

        merged = merge_transcript_lines(mic_lines, system_lines, screen_lines)
        transcript_text = render_transcript(merged)

        for line in merged:
            self._db.add_transcript_segment(
                self.meeting_id, line.source, line.timestamp_seconds, line.text
            )

        if self._settings.copilot_sync_dir is not None:
            manual_notes = self._db.get_meeting(self.meeting_id).manual_notes
            content = format_meeting_for_push(
                project_name=self.project.name,
                title=self.title,
                transcript_text=transcript_text,
                manual_notes=manual_notes,
            )
            push_meeting(content, inbox_dir=self._settings.copilot_inbox_dir)
            report("Pushed to Copilot Studio.")
        else:
            report("Copilot sync folder not configured — nothing pushed.")

        self._db.finish_meeting(self.meeting_id, transcript_text=transcript_text)
        report("Meeting saved.")
        return transcript_text
