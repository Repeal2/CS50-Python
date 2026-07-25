"""Orchestrates one meeting end to end: start recording (mic + system audio + screen), and on stop,
transcribe, merge into one transcript, generate notes via Claude, and file everything under the
meeting's project so it's searchable later.
"""

from __future__ import annotations

from meeting_scribe.ai.notes import generate_notes
from meeting_scribe.audio.recorder import Recorder
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent, ScreenWatcher
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
        screen_target: WindowTarget | None = None,
    ):
        """`screen_target` selects a single window to OCR instead of the whole screen — e.g. just the
        Teams/Zoom window. Leave it None to capture the whole screen."""
        self._settings = settings
        self._db = db
        self.project = db.get_or_create_project(project_name)
        self.meeting_id = db.create_meeting(self.project.id, title)

        meeting_dir = settings.meeting_dir(self.project.slug, self.meeting_id)
        meeting_dir.mkdir(parents=True, exist_ok=True)

        self._recorder = Recorder(meeting_dir)
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

    def stop(self) -> str:
        """Stops recording, transcribes, generates notes, and saves the meeting. Returns the notes
        (or the plain transcript if no Anthropic API key is configured)."""
        self._screen_watcher.stop()
        recorded = self._recorder.stop()

        mic_lines = self._transcriber.transcribe(recorded.mic_path, source="mic")
        system_lines = self._transcriber.transcribe(recorded.system_path, source="system")
        screen_lines = [
            TranscriptLine(event.timestamp_seconds, "screen_ocr", event.text)
            for event in self._screen_events
        ]

        merged = merge_transcript_lines(mic_lines, system_lines, screen_lines)
        transcript_text = render_transcript(merged)

        for line in merged:
            self._db.add_transcript_segment(
                self.meeting_id, line.source, line.timestamp_seconds, line.text
            )

        notes = None
        if self._settings.anthropic_api_key:
            notes = generate_notes(
                transcript_text,
                api_key=self._settings.anthropic_api_key,
                model=self._settings.anthropic_model,
            )

        self._db.finish_meeting(self.meeting_id, transcript_text=transcript_text, notes_markdown=notes)
        return notes or transcript_text
