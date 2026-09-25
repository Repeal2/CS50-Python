"""Local speech-to-text (faster-whisper) plus the merge that turns two audio tracks and a screen-OCR
event stream into one time-ordered meeting transcript.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

SOURCE_LABELS = {"mic": "You", "system": "Others", "screen_ocr": "Screen"}

# Rough resident-memory footprints for faster-whisper's int8 CPU inference, in MB, per model size —
# enough to catch "this is very likely to fail" before it does, not a precise measurement (actual usage
# depends on audio length, thread count, and everything else the process is holding at the time). Adapted
# from whisper.cpp's published memory table, scaled down somewhat for int8's smaller footprint relative
# to that table's fp16-class numbers. See config.WHISPER_MODEL_SIZES for the sizes the Settings page
# actually offers.
_APPROXIMATE_MODEL_MEMORY_MB = {
    "tiny": 300,
    "base": 400,
    "small": 700,
    "medium": 1800,
    "large-v3": 3200,
    "large-v3-turbo": 1800,
}
# Used for a model name that isn't in the table above (e.g. a distil-*/.en variant set directly via
# MEETING_SCRIBE_WHISPER_MODEL rather than picked from the Settings page) — assumes something in the
# "small" ballpark rather than skipping the check entirely for an unrecognized name.
_DEFAULT_APPROXIMATE_MODEL_MEMORY_MB = 700

# Warn once free memory drops below this multiple of the model's own footprint — leaving headroom for
# everything else the process (screen capture, the GUI, Windows itself) needs, not just the model weights.
_LOW_MEMORY_SAFETY_MARGIN = 1.5


def available_memory_mb() -> float | None:
    """Free physical memory available right now, in MB — or None if it can't be determined (this isn't
    Windows, or the OS call itself fails). Meant only to catch an "almost certainly not enough" case
    before starting transcription (see low_memory_warning), not to make any hard decision — a reading
    that's a second stale by the time transcription actually starts doesn't get to block anything.

    Calls the Win32 GlobalMemoryStatusEx API directly via ctypes rather than adding a psutil dependency
    for one number — this app already reaches for ctypes.windll for exactly this kind of one-off Windows
    API call (see gui.app._enable_per_monitor_dpi_awareness)."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except OSError:
        return None
    return (status.ullAvailPhys / (1024 * 1024)) if succeeded else None


def low_memory_warning(model_size: str, available_mb: float | None) -> str | None:
    """A ready-to-show warning if there's reason to think transcription is about to fail for lack of
    memory (the `mkl_malloc: failed to allocate memory` failure mode this exists to give a heads-up about
    before it happens), or None if things look fine — or `available_mb` is None, e.g. off Windows, where
    silence is safer than a false alarm. A heuristic, not a guarantee: it can't see what else might
    allocate memory between this check and the real work, so a clean result here isn't a promise nothing
    will go wrong, and a warning here doesn't necessarily mean it will."""
    if available_mb is None:
        return None
    required_mb = _APPROXIMATE_MODEL_MEMORY_MB.get(model_size, _DEFAULT_APPROXIMATE_MODEL_MEMORY_MB)
    if available_mb >= required_mb * _LOW_MEMORY_SAFETY_MARGIN:
        return None
    return (
        f"Low memory warning: only about {available_mb:.0f} MB free, and the '{model_size}' "
        f"transcription model typically needs on the order of {required_mb} MB while running. "
        "Transcription may fail with an out-of-memory error — closing other applications, or switching "
        "to a smaller model in Settings, reduces the risk."
    )


# One transcription at a time, process-wide. Back-to-back meetings (session.py) and a Projects-tab retry
# each run on their own background thread with their own WhisperTranscriber, and each loads its own copy
# of the model — two at once doubles the memory needed, which is exactly the `mkl_malloc: failed to
# allocate memory` abort (a native crash that takes the whole app down, not a Python exception) the low-
# memory warning exists for. Queuing them costs some latency on the second meeting, never its transcript.
_transcription_lock = threading.Lock()


@contextlib.contextmanager
def transcription_slot(on_wait: Callable[[], None] | None = None) -> Iterator[None]:
    """Holds the process-wide transcription slot for the duration of the block. `on_wait` is called
    once, before blocking, if another transcription already holds it — so the caller can say why
    nothing seems to be happening."""
    if not _transcription_lock.acquire(blocking=False):
        if on_wait is not None:
            on_wait()
        _transcription_lock.acquire()
    try:
        yield
    finally:
        _transcription_lock.release()


@dataclass(frozen=True)
class TranscriptLine:
    timestamp_seconds: float
    source: str  # "mic" | "system" | "screen_ocr"
    text: str
    # Set only for a diarized system-track line (see transcription.runpod_whisperx) — a per-line speaker
    # id ("SPEAKER_00") that render_transcript prefers over the flat per-source SOURCE_LABELS lookup.
    # None for every other line, including a system-track line transcribed locally, where there's no
    # per-speaker distinction to carry.
    speaker: str | None = None


class WhisperTranscriber:
    """Thin wrapper around faster-whisper. The model is loaded lazily on first use so importing this
    module (or even constructing a transcriber) doesn't require the model weights to be present."""

    def __init__(self, model_size: str = "small"):
        self._model_size = model_size
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            # device="cpu" is deliberate: with no device specified, ctranslate2 auto-detects and will
            # try CUDA on any machine with an NVIDIA GPU, then fail trying to load cuBLAS — we don't
            # bundle the CUDA runtime (it would add gigabytes for a benefit most users can't use), so
            # CPU is the only device this app actually supports.
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    def unload(self) -> None:
        """Drops the loaded model so its memory can be reclaimed as soon as this transcriber is done,
        rather than whenever the session holding it is garbage-collected — the next queued transcription
        (see transcription_slot) shouldn't have to share memory with a model nobody is using anymore."""
        self._model = None

    def transcribe(self, audio_path: Path, source: str) -> list[TranscriptLine]:
        """Runs Whisper over a recorded WAV file, returning one TranscriptLine per detected segment."""
        model = self._ensure_model()
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
        return [
            TranscriptLine(timestamp_seconds=segment.start, source=source, text=segment.text.strip())
            for segment in segments
            if segment.text.strip()
        ]

    def transcribe_parts(
        self,
        audio_paths: Sequence[Path],
        source: str,
        start_offsets: Mapping[Path, float] | None = None,
    ) -> list[TranscriptLine]:
        """Transcribes one capture stream that may have been written as several WAV parts.

        A stream rolls over into a new part whenever its device changes, and when a long meeting would
        overflow a WAV header (see audio.recorder). Whisper timestamps each part from its own zero, so
        each part is shifted to where it started on the meeting's clock — see part_start_offsets."""
        lines: list[TranscriptLine] = []
        for audio_path, offset_seconds in zip(audio_paths, part_start_offsets(audio_paths, start_offsets)):
            lines.extend(
                TranscriptLine(line.timestamp_seconds + offset_seconds, line.source, line.text)
                for line in self.transcribe(audio_path, source=source)
            )
        return lines


def part_start_offsets(
    audio_paths: Sequence[Path], known: Mapping[Path, float] | None = None
) -> list[float]:
    """Where each WAV part of one capture stream starts on the meeting's clock, in seconds.

    `known` is what the recorder noted as each part opened (see audio.recorder.load_part_offsets) — the
    time that had actually passed since the meeting started. That's the only trustworthy answer: a part's
    own length says how much audio it holds, not how long it was recording for, and the two part ways
    whenever a device delivers more or less audio than real time (a Bluetooth headset changing profile,
    a driver's own resampling). Adding up part lengths instead used to push every line after such a part
    — sometimes by hours — past the end of the other track, so a whole track landed at the bottom of the
    transcript. A part the recorder didn't note (a meeting recorded before it did) falls back to starting
    where the previous one ended."""
    offsets: list[float] = []
    next_start = 0.0
    for audio_path in audio_paths:
        start = known.get(Path(audio_path)) if known else None
        if start is None:
            start = next_start
        offsets.append(start)
        next_start = start + wav_duration_seconds(audio_path)
    return offsets


def wav_duration_seconds(path: Path) -> float:
    """Length of a WAV file in seconds, or 0.0 if it can't be read — a missing or unreadable part
    shouldn't sink a transcript that otherwise came out fine."""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wav_file:
            framerate = wav_file.getframerate()
            return wav_file.getnframes() / framerate if framerate else 0.0
    except (OSError, EOFError, wave.Error):  # EOFError: a 0-byte file, which never got a header
        return 0.0


def merge_transcript_lines(*line_groups: list[TranscriptLine]) -> list[TranscriptLine]:
    """Interleaves mic / system / screen-OCR lines into one chronological transcript."""
    merged: list[TranscriptLine] = [line for group in line_groups for line in group]
    merged.sort(key=lambda line: line.timestamp_seconds)
    return merged


def render_transcript(lines: list[TranscriptLine]) -> str:
    """Renders merged transcript lines as readable `[mm:ss] Speaker: text` rows."""
    rows = []
    for line in lines:
        minutes, seconds = divmod(max(0, int(line.timestamp_seconds)), 60)
        label = line.speaker or SOURCE_LABELS.get(line.source, line.source)
        rows.append(f"[{minutes:02d}:{seconds:02d}] {label}: {line.text}")
    return "\n".join(rows)
