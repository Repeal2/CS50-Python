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

from pathlib import Path
from typing import Callable

from meeting_scribe.ai.copilot_push import ReferenceDocument, TextReferenceDocument, push_meeting_package
from meeting_scribe.audio.recorder import Recorder
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent, ScreenWatcher
from meeting_scribe.screen.region_picker import RegionTarget, WindowRegionTarget
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
        screen_target: WindowTarget | RegionTarget | WindowRegionTarget | None = None,
    ):
        """`screen_target` selects a single window, a fixed user-drawn rectangle, or a user-drawn
        rectangle pinned to a window's current position, to OCR instead of the whole screen — e.g. just
        the Teams/Zoom window, just a captions bar, or just a corner of the Teams window that keeps
        capturing that same corner even if the window is moved to another monitor. Leave it None to
        capture the whole screen."""
        self._settings = settings
        self._db = db
        self.project = db.get_or_create_project(project_name)
        self.meeting_id = db.create_meeting(self.project.id, title)
        self.title = title
        # The Copilot push package's file names are keyed on this (see ai/copilot_push.py), not the
        # database row id — assigned once at creation and never changes, unlike the title.
        self.meeting_code = db.get_meeting(self.meeting_id).meeting_code

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

    def capture_errors(self) -> tuple[str, ...]:
        """Anything that has killed one of the recorder's capture threads so far, newest last — empty
        while both streams are healthy. The GUI polls this alongside audio_levels() so a microphone that
        drops out mid-meeting is visible immediately, not a surprise in the finished transcript."""
        return self._recorder.capture_errors()

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
        # A capture thread that died mid-meeting (device unplugged, disk full, a driver error) leaves
        # the rest of that track silent. Say so rather than letting a half-recorded meeting look like a
        # quiet one — the transcript that comes out of it is genuinely incomplete.
        for message in recorded.errors:
            report(message)

        mic_lines = self._transcriber.transcribe_parts(recorded.mic_paths, source="mic")
        system_lines = self._transcriber.transcribe_parts(recorded.system_paths, source="system")
        screen_lines = [
            TranscriptLine(event.timestamp_seconds, "screen_ocr", event.text)
            for event in self._screen_events
        ]
        report("Transcription complete.")

        # The Copilot push package (see ai/copilot_push.py) hands off the spoken and on-screen text as
        # two separate files, not one merged one — audio_lines covers both directions of the call (mic
        # and system), screen_lines is the OCR stream. The combined `merged` rendering is still what's
        # kept in this app's own local record (finish_meeting below), where mixing sources by timestamp
        # is exactly the point.
        audio_lines = merge_transcript_lines(mic_lines, system_lines)
        merged = merge_transcript_lines(audio_lines, screen_lines)
        transcript_text = render_transcript(merged)

        for line in merged:
            self._db.add_transcript_segment(
                self.meeting_id, line.source, line.timestamp_seconds, line.text
            )

        if self._settings.copilot_sync_dir is not None:
            meeting = self._db.get_meeting(self.meeting_id)
            # Manual notes and the OCR'd attendee list aren't uploaded files, but they're handed off the
            # same reference-doc-style way as one — same naming, same manifest shape — rather than being
            # folded into either transcript, since neither is really "audio" or "on-screen text".
            text_reference_documents = []
            if meeting.manual_notes and meeting.manual_notes.strip():
                text_reference_documents.append(
                    TextReferenceDocument(original_filename="meeting-notes.txt", content=meeting.manual_notes)
                )
            if meeting.attendees and meeting.attendees.strip():
                text_reference_documents.append(
                    TextReferenceDocument(original_filename="attendees.txt", content=meeting.attendees)
                )
            reference_documents = [
                ReferenceDocument(original_filename=row["filename"], source_path=Path(row["source_path"]))
                for row in self._db.list_documents_for_meeting(self.meeting_id)
                if row["source_path"]
            ]
            push_meeting_package(
                meeting_code=self.meeting_code,
                project_name=self.project.name,
                meeting_title=self.title,
                audio_transcript_text=render_transcript(audio_lines),
                screen_transcript_text=render_transcript(screen_lines),
                reference_documents=reference_documents,
                text_reference_documents=text_reference_documents,
                inbox_dir=self._settings.copilot_inbox_dir,
            )
            report("Pushed to Copilot Studio.")
        else:
            report("Copilot sync folder not configured — nothing pushed.")

        self._db.finish_meeting(self.meeting_id, transcript_text=transcript_text)
        report("Meeting saved.")
        return transcript_text
