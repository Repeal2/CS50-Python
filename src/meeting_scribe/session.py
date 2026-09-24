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

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from meeting_scribe.ai.copilot_push import ReferenceDocument, TextReferenceDocument, push_meeting_package
from meeting_scribe.audio.recorder import Recorder, discover_wav_parts
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent, ScreenWatcher, SpeakerNameEvent
from meeting_scribe.screen.region_picker import RegionTarget, WindowRegionTarget
from meeting_scribe.screen.window_picker import WindowTarget
from meeting_scribe.storage.database import Database
from meeting_scribe.transcription.engine import (
    TranscriptLine,
    WhisperTranscriber,
    available_memory_mb,
    low_memory_warning,
    merge_transcript_lines,
    render_transcript,
    transcription_slot,
    wav_duration_seconds,
)

_WAITING_FOR_TRANSCRIPTION_MESSAGE = "Waiting for another meeting's transcription to finish first…"


@dataclass(frozen=True)
class _SystemTranscripts:
    local: list[TranscriptLine]
    # None when cloud diarization is off, or failed.
    runpod: list[TranscriptLine] | None

    @property
    def primary(self) -> list[TranscriptLine]:
        """The one used for the meeting's own transcript and the Copilot push: Runpod's when there is
        one, since its speaker labels are the point of opting in."""
        return self.runpod if self.runpod is not None else self.local


def _transcribe_system_track(
    settings: Settings,
    local_transcriber: WhisperTranscriber,
    system_paths: Sequence[Path],
    report: Callable[[str], None],
) -> _SystemTranscripts:
    """Transcribes the system-audio track — everyone but the meeting owner — on this PC, and also with
    cloud speaker diarization when opted into (Settings.diarize_system_audio), so the two can be
    compared side by side on the Projects tab. A cloud failure is reported rather than raised: a
    third-party outage or a missing API key shouldn't cost a meeting its transcript, just the per-speaker
    labels."""
    if not system_paths:
        return _SystemTranscripts(local=[], runpod=None)
    runpod_lines = None
    if settings.diarize_system_audio:
        try:
            from meeting_scribe.transcription.runpod_whisperx import (
                RunpodWhisperXError,
                RunpodWhisperXTranscriber,
            )

            transcriber = RunpodWhisperXTranscriber(
                api_key=settings.runpod_api_key,
                endpoint_id=settings.runpod_endpoint_id,
                huggingface_token=settings.runpod_huggingface_token,
                on_progress=report,
            )
            runpod_lines = transcriber.transcribe_parts(system_paths, source="system")
        except RunpodWhisperXError as error:
            report(f"Cloud speaker diarization failed ({error}) — using the local transcript only.")
        else:
            report("Transcribing system audio on this PC too, for comparison…")
    local_lines = local_transcriber.transcribe_parts(system_paths, source="system")
    return _SystemTranscripts(local=local_lines, runpod=runpod_lines)


def _parts_with_audio(paths: Sequence[Path], label: str, report: Callable[[str], None]) -> tuple[Path, ...]:
    """The WAV parts that actually contain audio. One that's missing, empty or unreadable — e.g. a
    capture stream whose device never delivered anything (see Recorder.stop) — is left out with a
    message, rather than failing transcription for the whole meeting, including the track that's fine."""
    kept = []
    for path in paths:
        if wav_duration_seconds(path) > 0:
            kept.append(path)
        else:
            report(f"{label}: {path.name} has no audio in it, so that track is left out of the transcript.")
    return tuple(kept)


def _save_segments(
    db: Database,
    meeting_id: int,
    mic_lines: list[TranscriptLine],
    system: _SystemTranscripts,
    screen_lines: list[TranscriptLine],
) -> None:
    """Saves every line, tagged with the transcriber that produced it, so the Projects tab can show the
    local and Runpod versions of the system audio side by side (each alongside the same mic lines)."""
    for line in screen_lines:
        db.add_transcript_segment(meeting_id, line.source, line.timestamp_seconds, line.text)
    for line in mic_lines + system.local:
        db.add_transcript_segment(meeting_id, line.source, line.timestamp_seconds, line.text, engine="local")
    for line in system.runpod or []:
        db.add_transcript_segment(
            meeting_id, line.source, line.timestamp_seconds, line.text, speaker=line.speaker, engine="runpod"
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

        meeting_dir = settings.meeting_dir(self.meeting_id)
        meeting_dir.mkdir(parents=True, exist_ok=True)

        self._recorder = Recorder(
            meeting_dir,
            mic_device_name=settings.mic_device_name,
            system_device_name=settings.system_device_name,
        )
        self._screen_events: list[ScreenTextEvent] = []
        # Timestamped speaker-name-badge sightings, kept alongside the caption stream — not used for
        # anything yet, but this is the raw material a future system-audio diarization pass would line up
        # against WhisperX speaker clusters to turn "SPEAKER_00" into a real name.
        self._speaker_name_events: list[SpeakerNameEvent] = []
        self._screen_watcher = ScreenWatcher(
            on_text=self._screen_events.append,
            on_speaker_name=self._speaker_name_events.append,
            interval_seconds=settings.screen_capture_interval_seconds,
            tesseract_cmd=settings.tesseract_cmd,
            target=screen_target,
        )
        self._transcriber = WhisperTranscriber(model_size=settings.whisper_model_size)

    def start(self) -> None:
        self._recorder.start()
        self._screen_watcher.start()

    def switch_mic_device(self, device_name: str | None) -> None:
        """Moves the mic recording to a different device mid-meeting instead of the old silent no-op
        (picking a new device used to only change the *next* meeting's default — see Recorder for why
        that used to produce a full-length recording of whatever device was originally opened, with no
        error, if the user switched mics partway through)."""
        self._recorder.switch_mic_device(device_name)

    def switch_system_device(self, device_name: str | None) -> None:
        """Moves the system-audio recording to a different device mid-meeting. See switch_mic_device."""
        self._recorder.switch_system_device(device_name)

    def set_project(self, project_name: str) -> None:
        """Moves this meeting to a different project, any time before it's finished — a reassignment,
        not a copy: the meeting keeps its id, title, and transcript-in-progress. Creates the project if
        `project_name` hasn't been used before, exactly like starting a meeting under one does.

        A pure database update with nothing to move on disk: recording storage is keyed by meeting id
        alone, not by project (see Settings.meeting_dir), specifically so this stays safe to call while
        the Recorder is still actively writing to those files."""
        new_project = self._db.get_or_create_project(project_name)
        self._db.move_meeting_to_project(self.meeting_id, new_project.id)
        self.project = new_project

    def reload_devices(self) -> None:
        """Re-reads the audio device list mid-meeting so newly connected devices become selectable — see
        Recorder.reload_devices. Blocks briefly while both streams restart; call off the GUI thread."""
        self._recorder.reload_devices()

    def available_devices(self) -> tuple[list, list]:
        """(input devices, loopback devices) as currently seen by this meeting's recorder — see
        Recorder.available_devices for why this, not a fresh enumeration, is the current list."""
        return self._recorder.available_devices()

    @property
    def device_list_version(self) -> int:
        """Changes whenever the recorder has reloaded its device list (see Recorder.reload_devices)."""
        return self._recorder.device_list_version

    def audio_levels(self) -> tuple[float, float]:
        """Current (mic, system) input levels, roughly 0..1 — lets the GUI show a live "is this actually
        picking up audio" meter while recording. Both are 0.0 before start() or after stop()."""
        return self._recorder.mic_level, self._recorder.system_level

    def capture_notices(self) -> tuple[str, ...]:
        """Anything the recorder wants said about this recording so far, newest last — a capture thread
        that died, a selected device Windows no longer has. Empty while nothing is wrong. The GUI polls
        this alongside audio_levels() so a microphone that drops out mid-meeting is visible immediately,
        not a surprise in the finished transcript."""
        return self._recorder.capture_notices()

    def input_problems(self) -> tuple[str, ...]:
        """What currently looks wrong with what's being captured — a microphone producing digital
        silence, an input that has heard nothing while the other track was busy, a clipping device.
        Empty while both inputs look healthy; recomputed per call, so it clears if the input recovers."""
        return self._recorder.input_problems()

    def abandon(self) -> None:
        """Stops capturing without transcribing — for the app closing mid-meeting. The WAV files are
        finalized on disk and the meeting is left unfinished (no transcript), which is exactly the state
        retry_meeting_transcription picks up from later. On-screen OCR text is lost, same as for any
        other retry."""
        self._screen_watcher.stop()
        self._recorder.stop()

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
        # A capture thread that died mid-meeting (device unplugged, disk full, a driver error), a
        # device that had to be substituted, an input that never picked anything up — all of it lands
        # here. Say so rather than letting a half-recorded meeting look like a quiet one; the transcript
        # that comes out of it is genuinely incomplete, and the reason is worth having on the record.
        for message in recorded.notices:
            report(message)

        mic_paths = _parts_with_audio(recorded.mic_paths, "Microphone", report)
        system_paths = _parts_with_audio(recorded.system_paths, "System audio", report)
        with transcription_slot(on_wait=lambda: report(_WAITING_FOR_TRANSCRIPTION_MESSAGE)):
            # Checked right before the expensive part starts, not any earlier — a warning here is what a
            # `mkl_malloc: failed to allocate memory` crash further down would otherwise give no advance
            # notice of at all (see transcription.engine.low_memory_warning).
            warning = low_memory_warning(self._settings.whisper_model_size, available_memory_mb())
            if warning is not None:
                report(warning)

            try:
                mic_lines = self._transcriber.transcribe_parts(mic_paths, source="mic")
                system = _transcribe_system_track(self._settings, self._transcriber, system_paths, report)
            finally:
                self._transcriber.unload()
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
        audio_lines = merge_transcript_lines(mic_lines, system.primary)
        merged = merge_transcript_lines(audio_lines, screen_lines)
        transcript_text = render_transcript(merged)

        _save_segments(self._db, self.meeting_id, mic_lines, system, screen_lines)

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


def retry_meeting_transcription(
    settings: Settings,
    db: Database,
    meeting_id: int,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Re-runs transcription for a meeting whose recording finished but whose transcription didn't — the
    situation left behind by, say, a `mkl_malloc: failed to allocate memory` crash partway through a long
    meeting (transcription is the expensive, failure-prone step; MeetingSession.stop() already writes the
    mic/system WAV files to disk before attempting it). This works from those files directly, so nothing
    has to be re-recorded.

    On-screen OCR text isn't recoverable this way: it only ever lived in the failed MeetingSession's
    in-memory `_screen_events` list, which was lost along with that session, so a retried transcript
    covers spoken audio only — the GUI should make that limitation visible rather than presenting the
    result as a complete redo.

    Raises ValueError if the meeting (or its project) doesn't exist, or if the meeting already has a
    transcript (nothing to retry); raises FileNotFoundError if no recorded audio can be found for it.
    """

    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"No meeting with id {meeting_id}")
    if meeting.ended_at is not None:
        raise ValueError(f'"{meeting.title}" already has a transcript — nothing to retry.')
    project = db.get_project(meeting.project_id)
    if project is None:
        raise ValueError(f"Meeting {meeting_id}'s project no longer exists")

    meeting_dir = settings.meeting_dir(meeting_id)
    mic_paths = discover_wav_parts(meeting_dir / "mic.wav")
    system_paths = discover_wav_parts(meeting_dir / "system.wav")
    if not mic_paths and not system_paths:
        raise FileNotFoundError(
            f'No recorded audio found for "{meeting.title}" in {meeting_dir} — nothing to retranscribe.'
        )
    mic_paths = _parts_with_audio(mic_paths, "Microphone", report)
    system_paths = _parts_with_audio(system_paths, "System audio", report)
    if not mic_paths and not system_paths:
        raise FileNotFoundError(
            f'The recorded audio for "{meeting.title}" in {meeting_dir} is empty — nothing to retranscribe.'
        )

    with transcription_slot(on_wait=lambda: report(_WAITING_FOR_TRANSCRIPTION_MESSAGE)):
        warning = low_memory_warning(settings.whisper_model_size, available_memory_mb())
        if warning is not None:
            report(warning)

        transcriber = WhisperTranscriber(model_size=settings.whisper_model_size)
        try:
            mic_lines = transcriber.transcribe_parts(mic_paths, source="mic")
            system = _transcribe_system_track(settings, transcriber, system_paths, report)
        finally:
            transcriber.unload()
    report("Transcription complete.")

    audio_lines = merge_transcript_lines(mic_lines, system.primary)
    transcript_text = render_transcript(audio_lines)

    # Clears any segments a previous, partially-successful retry left behind (e.g. one that transcribed
    # fine but then failed pushing to Copilot Studio) before inserting this attempt's — otherwise a
    # second retry would leave both attempts' segments sitting side by side.
    db.clear_transcript_segments(meeting_id)
    _save_segments(db, meeting_id, mic_lines, system, screen_lines=[])

    if settings.copilot_sync_dir is not None:
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
            for row in db.list_documents_for_meeting(meeting_id)
            if row["source_path"]
        ]
        push_meeting_package(
            meeting_code=meeting.meeting_code,
            project_name=project.name,
            meeting_title=meeting.title,
            audio_transcript_text=transcript_text,
            screen_transcript_text="",
            reference_documents=reference_documents,
            text_reference_documents=text_reference_documents,
            inbox_dir=settings.copilot_inbox_dir,
        )
        report("Pushed to Copilot Studio.")
    else:
        report("Copilot sync folder not configured — nothing pushed.")

    db.finish_meeting(meeting_id, transcript_text=transcript_text)
    report("Meeting saved.")
    return transcript_text
