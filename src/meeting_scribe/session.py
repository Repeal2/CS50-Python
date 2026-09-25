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

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from meeting_scribe.ai.copilot_push import ReferenceDocument, TextReferenceDocument, push_meeting_package
from meeting_scribe.audio.recorder import Recorder, discover_wav_parts, load_part_offsets
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent, ScreenWatcher
from meeting_scribe.screen.region_picker import RegionTarget
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

# The on-screen text a meeting's OCR picked up, one JSON line per event, written as each one arrives —
# see _ScreenTextLog.
SCREEN_TEXT_FILENAME = "screen_text.jsonl"

# Meetings being recorded, finished (transcribed, pushed, saved) or retried right now. Until one of these
# is done, its database row looks exactly like a meeting whose transcription failed (no end time, no
# transcript) — which is what the Library offers Retry for. Retrying a meeting that's still being
# recorded transcribed the audio captured so far, sent it to Runpod partway through the call, and marked
# the meeting finished with that partial transcript; so a meeting in here can't be retried.
_meetings_in_progress: set[int] = set()
_meetings_in_progress_lock = threading.Lock()


def meeting_in_progress(meeting_id: int) -> bool:
    """Whether this meeting is still being recorded, finished or retried in this process."""
    with _meetings_in_progress_lock:
        return meeting_id in _meetings_in_progress


def _claim_meeting(meeting_id: int) -> bool:
    with _meetings_in_progress_lock:
        if meeting_id in _meetings_in_progress:
            return False
        _meetings_in_progress.add(meeting_id)
        return True


def _release_meeting(meeting_id: int) -> None:
    with _meetings_in_progress_lock:
        _meetings_in_progress.discard(meeting_id)


class _ScreenTextLog:
    """Keeps a meeting's OCR events both in memory and in a file beside its audio, appended as each one
    arrives. They used to live only in memory until the meeting finished, so anything that stopped the
    meeting from finishing — a transcription crash, the app being closed — lost every on-screen line,
    and a retried transcript could only ever cover the audio. Written line by line and flushed each
    time, so a crash loses at most the line being written."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self.events: list[ScreenTextEvent] = []

    def append(self, event: ScreenTextEvent) -> None:
        with self._lock:
            self.events.append(event)
            try:
                with open(self._path, "a", encoding="utf-8") as file:
                    file.write(json.dumps({"t": event.timestamp_seconds, "text": event.text}) + "\n")
            except OSError:
                pass  # the in-memory copy still makes it into the transcript

    def snapshot(self) -> list[ScreenTextEvent]:
        with self._lock:
            return list(self.events)


def load_screen_text_events(meeting_dir: Path) -> list[ScreenTextEvent]:
    """The OCR events a meeting saved as it went (see _ScreenTextLog) — empty if there are none. A line
    that can't be read (the last one, cut off by a crash) is skipped rather than failing the rest."""
    path = meeting_dir / SCREEN_TEXT_FILENAME
    events: list[ScreenTextEvent] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            record = json.loads(line)
            events.append(ScreenTextEvent(float(record["t"]), str(record["text"])))
        except (ValueError, KeyError, TypeError):
            continue
    return events


# Which transcriber produced a spoken line — how segments are tagged in the database (see
# Database.add_transcript_segment), and the two transcripts a meeting can have side by side.
LOCAL_ENGINE = "local"
CLOUD_ENGINE = "runpod"
ENGINE_NAMES = {LOCAL_ENGINE: "on this PC", CLOUD_ENGINE: "in the cloud"}


def chosen_engines(settings: Settings) -> tuple[str, ...]:
    """The transcriptions Settings asks for, cloud first (it's the slower to come back, and runs outside
    the one-at-a-time local transcription slot). Never empty — see config.validate_transcription_choice."""
    engines = []
    if settings.transcribe_in_cloud:
        engines.append(CLOUD_ENGINE)
    if settings.transcribe_locally or not engines:
        engines.append(LOCAL_ENGINE)
    return tuple(engines)


@dataclass(frozen=True)
class MeetingTranscripts:
    """A meeting's saved lines, split the way the Library shows them: on-screen text, and the spoken
    transcript from each engine (microphone and system audio merged by time)."""

    screen: list[TranscriptLine]
    local: list[TranscriptLine]
    cloud: list[TranscriptLine]

    @property
    def preferred(self) -> list[TranscriptLine]:
        """The one transcript used where only one fits — the meeting's saved text, the Copilot push: the
        cloud's when there is one (it names the other side's speakers), this PC's otherwise."""
        return self.cloud or self.local


def meeting_transcripts(rows) -> MeetingTranscripts:
    """Splits a meeting's saved transcript rows (Database.get_segments) by engine. A line saved before
    lines were tagged by engine counts as this PC's. Meetings from before the microphone was also sent to
    the cloud have only the system track there; their cloud transcript borrows this PC's microphone
    lines, so it still reads as the whole conversation."""
    screen, local_mic, local_system, cloud_mic, cloud_system = [], [], [], [], []
    for row in rows:
        line = TranscriptLine(row["timestamp_seconds"], row["source"], row["text"], speaker=row["speaker"])
        if line.source == "screen_ocr":
            screen.append(line)
        elif row["engine"] == CLOUD_ENGINE:
            (cloud_mic if line.source == "mic" else cloud_system).append(line)
        else:
            (local_mic if line.source == "mic" else local_system).append(line)
    cloud = []
    if cloud_mic or cloud_system:
        cloud = merge_transcript_lines(cloud_mic or local_mic, cloud_system)
    local = merge_transcript_lines(local_mic, local_system)
    return MeetingTranscripts(screen=screen, local=local, cloud=cloud)


class _Tracks:
    """One meeting's recorded audio: each stream's parts, and where each part started."""

    def __init__(self, mic_paths, system_paths, mic_offsets=None, system_offsets=None):
        self.mic_paths, self.system_paths = tuple(mic_paths), tuple(system_paths)
        self.mic_offsets, self.system_offsets = mic_offsets, system_offsets


def _transcribe_in_cloud(
    settings: Settings, tracks: _Tracks, report: Callable[[str], None]
) -> tuple[list[TranscriptLine], list[TranscriptLine]]:
    """(microphone lines, system lines) from Runpod — the system track with its speakers labelled.
    Raises RunpodWhisperXError for anything that stops that, including missing credentials."""
    from meeting_scribe.transcription.runpod_whisperx import RunpodWhisperXTranscriber

    transcriber = RunpodWhisperXTranscriber(
        api_key=settings.runpod_api_key,
        endpoint_id=settings.runpod_endpoint_id,
        huggingface_token=settings.runpod_huggingface_token,
        on_progress=report,
    )
    system_lines = mic_lines = []
    if tracks.system_paths:
        system_lines = transcriber.transcribe_parts(
            tracks.system_paths, source="system", start_offsets=tracks.system_offsets
        )
    if tracks.mic_paths:
        mic_lines = transcriber.transcribe_parts(
            tracks.mic_paths, source="mic", start_offsets=tracks.mic_offsets, diarize=False
        )
    return mic_lines, system_lines


def _transcribe_locally(
    settings: Settings, transcriber: WhisperTranscriber, tracks: _Tracks, report: Callable[[str], None]
) -> tuple[list[TranscriptLine], list[TranscriptLine]]:
    with transcription_slot(on_wait=lambda: report(_WAITING_FOR_TRANSCRIPTION_MESSAGE)):
        # Checked right before the expensive part starts, not any earlier — a warning here is what a
        # `mkl_malloc: failed to allocate memory` crash further down would otherwise give no advance
        # notice of at all (see transcription.engine.low_memory_warning).
        warning = low_memory_warning(settings.whisper_model_size, available_memory_mb())
        if warning is not None:
            report(warning)
        try:
            mic_lines = system_lines = []
            if tracks.mic_paths:
                mic_lines = transcriber.transcribe_parts(
                    tracks.mic_paths, source="mic", start_offsets=tracks.mic_offsets
                )
            if tracks.system_paths:
                system_lines = transcriber.transcribe_parts(
                    tracks.system_paths, source="system", start_offsets=tracks.system_offsets
                )
        finally:
            transcriber.unload()
    return mic_lines, system_lines


def _transcribe(
    settings: Settings,
    transcriber: WhisperTranscriber,
    engines: Sequence[str],
    tracks: _Tracks,
    report: Callable[[str], None],
    *,
    fall_back: bool,
) -> dict[str, tuple[list[TranscriptLine], list[TranscriptLine]]]:
    """{engine: (microphone lines, system lines)} for each engine asked for. A cloud failure is reported
    rather than raised when `fall_back` is set: if this PC wasn't going to transcribe too, it does
    instead — a third-party outage or a missing API key shouldn't cost a meeting its transcript. Without
    `fall_back` (a transcription asked for by hand, after the fact) it's raised."""
    from meeting_scribe.transcription.runpod_whisperx import RunpodWhisperXError

    engines = list(engines)
    results: dict[str, tuple[list[TranscriptLine], list[TranscriptLine]]] = {}
    if CLOUD_ENGINE in engines:
        try:
            results[CLOUD_ENGINE] = _transcribe_in_cloud(settings, tracks, report)
        except RunpodWhisperXError as error:
            if not fall_back:
                raise
            if LOCAL_ENGINE in engines:
                report(f"Cloud transcription failed ({error}) — keeping this PC's transcript only.")
            else:
                report(f"Cloud transcription failed ({error}) — transcribing on this PC instead.")
                engines.append(LOCAL_ENGINE)
    if LOCAL_ENGINE in engines:
        results[LOCAL_ENGINE] = _transcribe_locally(settings, transcriber, tracks, report)
    return results


def _save_transcription(
    db: Database, meeting_id: int, engine: str, lines: tuple[list[TranscriptLine], list[TranscriptLine]]
) -> None:
    for line in (*lines[0], *lines[1]):
        db.add_transcript_segment(
            meeting_id, line.source, line.timestamp_seconds, line.text, speaker=line.speaker, engine=engine
        )


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


def _finish_meeting(
    settings: Settings,
    db: Database,
    meeting_id: int,
    *,
    transcriber: WhisperTranscriber,
    mic_paths: Sequence[Path],
    system_paths: Sequence[Path],
    screen_events: Sequence[ScreenTextEvent],
    report: Callable[[str], None],
    mic_offsets: Mapping[Path, float] | None = None,
    system_offsets: Mapping[Path, float] | None = None,
) -> str:
    """The shared back half of finishing a meeting — MeetingSession.stop() and
    retry_meeting_transcription() both end here: transcribe both tracks with each engine Settings asks
    for (see chosen_engines), save every line, push the package to Copilot Studio if a sync folder is set,
    and mark the meeting finished. Returns the merged transcript."""
    tracks = _Tracks(
        _parts_with_audio(mic_paths, "Microphone", report),
        _parts_with_audio(system_paths, "System audio", report),
        mic_offsets,
        system_offsets,
    )
    results = _transcribe(settings, transcriber, chosen_engines(settings), tracks, report, fall_back=True)
    report("Transcription complete.")

    # Clears whatever an earlier, partially-successful attempt left behind (one that transcribed fine but
    # then failed pushing, say) so a retry doesn't leave two attempts' lines side by side.
    db.clear_transcript_segments(meeting_id)
    for event in screen_events:
        db.add_transcript_segment(meeting_id, "screen_ocr", event.timestamp_seconds, event.text)
    for engine, lines in results.items():
        _save_transcription(db, meeting_id, engine, lines)

    transcripts = meeting_transcripts(db.get_segments(meeting_id))
    # The Copilot push hands off the spoken and on-screen text as two separate files; the local record
    # keeps them merged by timestamp.
    transcript_text = render_transcript(merge_transcript_lines(transcripts.preferred, transcripts.screen))
    if settings.copilot_sync_dir is not None:
        _push_to_copilot(settings, db, meeting_id, transcripts.preferred, transcripts.screen)
        report("Pushed to Copilot Studio.")
    else:
        report("Copilot sync folder not configured — nothing pushed.")

    db.finish_meeting(meeting_id, transcript_text=transcript_text)
    report("Meeting saved.")
    return transcript_text


def _push_to_copilot(
    settings: Settings,
    db: Database,
    meeting_id: int,
    audio_lines: list[TranscriptLine],
    screen_lines: list[TranscriptLine],
) -> None:
    meeting = db.get_meeting(meeting_id)
    project = db.get_project(meeting.project_id)
    # Manual notes and the OCR'd attendee list aren't uploaded files, but they're handed off the same
    # reference-doc-style way as one — same naming, same manifest shape — rather than being folded into
    # either transcript, since neither is really "audio" or "on-screen text".
    text_reference_documents = [
        TextReferenceDocument(original_filename=filename, content=content)
        for filename, content in (("meeting-notes.txt", meeting.manual_notes), ("attendees.txt", meeting.attendees))
        if content and content.strip()
    ]
    reference_documents = [
        ReferenceDocument(original_filename=row["filename"], source_path=Path(row["source_path"]))
        for row in db.list_documents_for_meeting(meeting_id)
        if row["source_path"]
    ]
    push_meeting_package(
        meeting_code=meeting.meeting_code,
        project_name=project.name,
        meeting_title=meeting.title,
        audio_transcript_text=render_transcript(audio_lines),
        screen_transcript_text=render_transcript(screen_lines),
        reference_documents=reference_documents,
        text_reference_documents=text_reference_documents,
        inbox_dir=settings.copilot_inbox_dir,
    )


class MeetingSession:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        project_name: str,
        title: str,
        screen_target: RegionTarget | None = None,
        *,
        read_screen: bool = True,
    ):
        """`screen_target` is the screen rectangle to OCR (where the OCR box is); None captures the whole
        screen.

        `read_screen=False` starts the meeting without reading anything on screen yet — the OCR box's
        flow (see screen.ocr_box): the box is put in place first, then its Start button calls
        set_screen_reading(True)."""
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
            auto_system_device=settings.auto_switch_audio_devices,
            auto_microphone=settings.auto_switch_audio_devices,
            headset_microphone_name=settings.headset_microphone_name,
        )
        self._screen_text = _ScreenTextLog(meeting_dir / SCREEN_TEXT_FILENAME)
        self._screen_watcher = ScreenWatcher(
            on_text=self._screen_text.append,
            interval_seconds=settings.screen_capture_interval_seconds,
            tesseract_cmd=settings.tesseract_cmd,
            target=screen_target,
            reading=read_screen,
        )
        self._transcriber = WhisperTranscriber(model_size=settings.whisper_model_size)

    def start(self) -> None:
        _claim_meeting(self.meeting_id)
        try:
            self._recorder.start()
        except BaseException:
            _release_meeting(self.meeting_id)
            raise
        self._screen_watcher.start()

    @property
    def screen_reading(self) -> bool:
        return self._screen_watcher.reading

    def set_screen_reading(self, reading: bool) -> None:
        """Starts or pauses reading on-screen text (the OCR box's Start/Stop button)."""
        self._screen_watcher.set_reading(reading)

    def set_screen_area(self, area: dict) -> None:
        """Reads from a different screen rectangle (an mss-style {left, top, width, height}) from the next
        capture on — the OCR box having been moved or resized."""
        self._screen_watcher.set_target(
            RegionTarget(left=area["left"], top=area["top"], width=area["width"], height=area["height"])
        )

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
        """Stops capturing without transcribing — for the app closing mid-meeting. The WAV files and the
        on-screen text log are finalized on disk and the meeting is left unfinished (no transcript),
        which is exactly the state retry_meeting_transcription picks up from later."""
        try:
            self._screen_watcher.stop()
            self._recorder.stop()
        finally:
            _release_meeting(self.meeting_id)

    def stop(
        self, on_progress: Callable[[str], None] | None = None, *, settings: Settings | None = None
    ) -> str:
        """Stops recording, transcribes, pushes the meeting to Copilot Studio (if configured), and saves
        it locally. Returns the plain transcript — there's no AI-generated notes to return anymore; that
        happens downstream, outside this app, once Copilot Studio picks up the pushed file.

        `on_progress`, if given, is called with a short human-readable status at each stage, since this
        whole method can take a while (local transcription isn't instant) and the caller (the GUI) uses
        it to keep an activity log current instead of the UI looking frozen with no feedback.

        `settings`, if given, is what the meeting is transcribed and pushed with — the Settings in force
        now, not when the meeting started. Without it, a transcription choice changed mid-meeting (the
        cloud unticked, say) only took effect from the next meeting, so this one still went to the cloud."""

        def report(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        if settings is not None:
            self._use_settings(settings)
        try:
            return self._stop(report)
        finally:
            _release_meeting(self.meeting_id)

    def _use_settings(self, settings: Settings) -> None:
        if settings.whisper_model_size != self._settings.whisper_model_size:
            self._transcriber = WhisperTranscriber(model_size=settings.whisper_model_size)
        self._settings = settings

    def _stop(self, report: Callable[[str], None]) -> str:
        self._screen_watcher.stop()
        if not self._screen_watcher.ever_read:
            report("On-screen text wasn't read — Start OCR was never pressed on the OCR box.")
        for message in self._screen_watcher.notices():
            report(message)
        recorded = self._recorder.stop()
        report("Recording stopped.")
        # A capture thread that died mid-meeting (device unplugged, disk full, a driver error), a
        # device that had to be substituted, an input that never picked anything up — all of it lands
        # here. Say so rather than letting a half-recorded meeting look like a quiet one; the transcript
        # that comes out of it is genuinely incomplete, and the reason is worth having on the record.
        for message in recorded.notices:
            report(message)

        return _finish_meeting(
            self._settings,
            self._db,
            self.meeting_id,
            transcriber=self._transcriber,
            mic_paths=recorded.mic_paths,
            system_paths=recorded.system_paths,
            mic_offsets=recorded.mic_offsets,
            system_offsets=recorded.system_offsets,
            screen_events=self._screen_text.snapshot(),
            report=report,
        )


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

    On-screen OCR text is picked back up from the log the meeting wrote as it went (see _ScreenTextLog);
    a meeting recorded before that log existed has none, and its retried transcript covers audio only.

    Raises ValueError if the meeting (or its project) doesn't exist, if the meeting already has a
    transcript (nothing to retry), or if it's still being recorded or finished (see
    meeting_in_progress); raises FileNotFoundError if no recorded audio can be found for it.
    """
    if not _claim_meeting(meeting_id):
        raise ValueError(
            "That meeting is still being recorded or finished — its transcript will appear when it's done."
        )
    try:
        return _retry_meeting_transcription(settings, db, meeting_id, on_progress)
    finally:
        _release_meeting(meeting_id)


def _retry_meeting_transcription(
    settings: Settings,
    db: Database,
    meeting_id: int,
    on_progress: Callable[[str], None] | None,
) -> str:

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

    return _finish_meeting(
        settings,
        db,
        meeting_id,
        transcriber=WhisperTranscriber(model_size=settings.whisper_model_size),
        mic_paths=mic_paths,
        system_paths=system_paths,
        mic_offsets=load_part_offsets(meeting_dir / "mic.wav"),
        system_offsets=load_part_offsets(meeting_dir / "system.wav"),
        screen_events=load_screen_text_events(meeting_dir),
        report=report,
    )


def add_transcription(
    settings: Settings,
    db: Database,
    meeting_id: int,
    engine: str,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Transcribes a finished meeting's recorded audio again with one engine — LOCAL_ENGINE or
    CLOUD_ENGINE — whatever Settings says: a meeting transcribed only on this PC can be sent to the cloud
    afterwards, or the other way round. Replaces that engine's lines if the meeting already has them,
    leaves the other engine's and the on-screen text alone, and updates the meeting's saved transcript
    (see MeetingTranscripts.preferred). Nothing is pushed to Copilot Studio again. Returns the new
    transcript, rendered.

    Raises ValueError if the meeting doesn't exist, hasn't finished (Retry is for that), or is being
    recorded or finished right now; FileNotFoundError if its audio isn't on disk any more; and
    RunpodWhisperXError if the cloud can't do it — there's no falling back when a specific engine was
    asked for."""
    if engine not in ENGINE_NAMES:
        raise ValueError(f"Unknown transcription engine {engine!r}")
    if not _claim_meeting(meeting_id):
        raise ValueError("That meeting is still being recorded or transcribed — try again when it's done.")
    try:
        report = on_progress or (lambda _message: None)
        meeting = db.get_meeting(meeting_id)
        if meeting is None:
            raise ValueError(f"No meeting with id {meeting_id}")
        if meeting.ended_at is None:
            raise ValueError(f"\"{meeting.title}\" hasn't finished transcribing — use Retry first.")
        meeting_dir = settings.meeting_dir(meeting_id)
        tracks = _Tracks(
            _parts_with_audio(discover_wav_parts(meeting_dir / "mic.wav"), "Microphone", report),
            _parts_with_audio(discover_wav_parts(meeting_dir / "system.wav"), "System audio", report),
            load_part_offsets(meeting_dir / "mic.wav"),
            load_part_offsets(meeting_dir / "system.wav"),
        )
        if not tracks.mic_paths and not tracks.system_paths:
            raise FileNotFoundError(
                f'The recorded audio for "{meeting.title}" is no longer in {meeting_dir} — nothing to transcribe.'
            )
        transcriber = WhisperTranscriber(model_size=settings.whisper_model_size)
        results = _transcribe(settings, transcriber, (engine,), tracks, report, fall_back=False)
        db.clear_transcript_segments(meeting_id, engine=engine)
        _save_transcription(db, meeting_id, engine, results[engine])

        transcripts = meeting_transcripts(db.get_segments(meeting_id))
        db.set_transcript_text(
            meeting_id, render_transcript(merge_transcript_lines(transcripts.preferred, transcripts.screen))
        )
        report(f"Transcribed {ENGINE_NAMES[engine]}.")
        return render_transcript(transcripts.cloud if engine == CLOUD_ENGINE else transcripts.local)
    finally:
        _release_meeting(meeting_id)
