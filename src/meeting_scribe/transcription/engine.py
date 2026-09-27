"""Local speech-to-text (faster-whisper) plus the merge that turns two audio tracks and a screen-OCR
event stream into one time-ordered meeting transcript.
"""

from __future__ import annotations

import contextlib
import re
import sys
import unicodedata
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence

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
    # English-only variants: the same weights' size as their multilingual counterparts.
    "base.en": 400,
    "small.en": 700,
    "medium.en": 1800,
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


# Whisper reads a prompt of at most 223 tokens (half its 448-token context, less one — see faster-whisper's
# WhisperModel.get_prompt, which cuts hotwords off there). A token is roughly four characters of English, so
# this keeps the whole list inside that window rather than letting the end of it be silently dropped.
_MAX_VOCABULARY_CHARS = 600


def build_vocabulary(*word_groups: Iterable[str | None]) -> str | None:
    """The words to steer Whisper's spelling towards — names, products, jargon — as one comma-separated
    string, or None if there are none. Groups are taken in order, so put the most important first: once
    the list reaches _MAX_VOCABULARY_CHARS, whatever comes after is left out. Blank entries and repeats
    (ignoring case) are dropped."""
    words: list[str] = []
    seen: set[str] = set()
    length = 0
    for group in word_groups:
        for word in group:
            word = " ".join((word or "").split())
            if not word or word.casefold() in seen:
                continue
            added = len(word) + (2 if words else 0)  # ", " between words
            if length + added > _MAX_VOCABULARY_CHARS:
                return ", ".join(words) or None
            words.append(word)
            seen.add(word.casefold())
            length += added
    return ", ".join(words) or None


def split_word_list(text: str | None) -> list[str]:
    """Splits a list typed by hand, or an attendee list read off the screen, into its entries — one per
    line, or separated by commas or semicolons."""
    return [part.strip() for part in re.split(r"[,;\n]", text or "") if part.strip()]


# Whisper was trained largely on subtitled video, so on silence, music or noise it tends to write out what
# a video's subtitles would have said there — credits, sign-offs, calls to subscribe. A segment whose whole
# text is one of these (compared after normalize_for_matching) is dropped wherever it comes from, this PC or
# the cloud. Deliberately only the phrases nobody says in a meeting — not "you" or "so", which are also
# common invented one-word segments but just as often real.
_INVENTED_PHRASES = frozenset(
    (
        "thank you for watching",
        "thanks for watching",
        "thank you so much for watching",
        "thank you for watching and see you next time",
        "please subscribe",
        "please like and subscribe",
        "like and subscribe",
        "subscribe to my channel",
        "dont forget to like and subscribe",
        "see you in the next video",
        "subtitles by the amara org community",
        "amara org",
        "gracias por ver el video",
        "subtitulos realizados por la comunidad de amara org",
        "merci davoir regarde cette video",
        "untertitel im auftrag des zdf fur funk 2017",
        "untertitel der amara org community",
    )
)
# ...and ones that only ever start a subtitle credit line, whatever follows ("Subtitles by <someone>").
_INVENTED_PREFIXES = (
    "subtitles by",
    "captions by",
    "transcribed by",
    "subtitulos realizados por",
    "sous titrage",
    "sous titres realises par",
    "untertitel im auftrag",
    "untertitelung",
)

# faster-whisper's own per-segment confidence signals, with the thresholds Whisper itself uses to decide a
# window was silence (no_speech_prob over 0.6 *and* avg_logprob under -1: the model both thinks there was
# no speech and wasn't sure of the words it wrote) or a stuck repetition (a compression_ratio over 2.4
# means the text is unusually repetitive — "the the the the"). Whisper retries such windows at a higher
# temperature but still returns what the last try produced.
_NO_SPEECH_PROB_MAX = 0.6
_AVG_LOGPROB_MIN = -1.0
_COMPRESSION_RATIO_MAX = 2.4


def normalize_for_matching(text: str) -> str:
    """Lowercase, accents and punctuation stripped, whitespace collapsed — "Thanks for watching!" and
    "thanks for watching" compare equal."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    plain = re.sub(r"['\u2019]", "", plain)  # "don't" -> "dont", "d'avoir" -> "davoir"
    plain = re.sub(r"[^\w\s]", " ", plain)  # "Amara.org" -> "amara org", "Sous-titrage" -> "sous titrage"
    return " ".join(plain.split())


def is_invented_line(text: str) -> bool:
    """Whether a transcript line is one of the stock phrases Whisper writes over silence or noise (see
    _INVENTED_PHRASES) rather than something said."""
    plain = normalize_for_matching(text)
    return plain in _INVENTED_PHRASES or plain.startswith(_INVENTED_PREFIXES)


def _is_unreliable_segment(segment) -> bool:
    """Whether faster-whisper's own confidence signals say this segment wasn't really speech, or is stuck
    repeating itself. A segment without the signals (never the case with faster-whisper itself) is kept."""
    no_speech_prob = getattr(segment, "no_speech_prob", None)
    avg_logprob = getattr(segment, "avg_logprob", None)
    compression_ratio = getattr(segment, "compression_ratio", None)
    if no_speech_prob is not None and avg_logprob is not None:
        if no_speech_prob > _NO_SPEECH_PROB_MAX and avg_logprob < _AVG_LOGPROB_MIN:
            return True
    return compression_ratio is not None and compression_ratio > _COMPRESSION_RATIO_MAX


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

    def transcribe(
        self,
        audio_path: Path,
        source: str,
        on_progress: Callable[[float], None] | None = None,
        *,
        language: str | None = None,
        vocabulary: str | None = None,
    ) -> list[TranscriptLine]:
        """Runs Whisper over a recorded WAV file, returning one TranscriptLine per detected segment.

        `on_progress`, if given, is called with how far into the file (in seconds) Whisper has got, as
        each segment comes out — faster-whisper decodes lazily as the segments are read, so this is live.

        `language` is an ISO code ("en"), or None to let Whisper guess from the first 30 seconds — which
        a part that opens on silence or hold music can get wrong, turning the whole part into another
        language. An English-only model (".en") is English whatever this says. `vocabulary` is words to
        spell the way they're given (see build_vocabulary).

        Each 30-second window is decoded on its own (condition_on_previous_text=False) rather than being
        prompted with the text before it: over a long meeting, conditioning lets one misheard line repeat
        itself for minutes, or text be invented across a quiet stretch. That also means an initial_prompt
        would only reach the first window, so the vocabulary goes in as hotwords, which faster-whisper
        puts in front of every window.

        Segments Whisper most likely invented are left out: ones its own confidence signals mark as
        silence or a stuck repetition, and stock subtitle phrases (see is_invented_line)."""
        model = self._ensure_model()
        segments, _info = model.transcribe(
            str(audio_path),
            vad_filter=True,
            condition_on_previous_text=False,
            language=language,
            hotwords=vocabulary,
        )
        lines = []
        for segment in segments:
            if on_progress is not None:
                on_progress(segment.end)
            if not segment.text.strip() or _is_unreliable_segment(segment) or is_invented_line(segment.text):
                continue
            lines.append(TranscriptLine(timestamp_seconds=segment.start, source=source, text=segment.text.strip()))
        return lines

    def transcribe_parts(
        self,
        audio_paths: Sequence[Path],
        source: str,
        start_offsets: Mapping[Path, float] | None = None,
        on_progress: Callable[[float], None] | None = None,
        *,
        language: str | None = None,
        vocabulary: str | None = None,
    ) -> list[TranscriptLine]:
        """Transcribes one capture stream that may have been written as several WAV parts.

        A stream rolls over into a new part whenever its device changes, and when a long meeting would
        overflow a WAV header (see audio.recorder). Whisper timestamps each part from its own zero, so
        each part is shifted to where it started on the meeting's clock — see part_start_offsets.

        `on_progress`, if given, is called with how many seconds of this stream's audio are done so far,
        across all its parts (see transcribe), which also says what `language` and `vocabulary` do."""
        lines: list[TranscriptLine] = []
        done_seconds = 0.0
        for audio_path, offset_seconds in zip(audio_paths, part_start_offsets(audio_paths, start_offsets)):
            part_progress = None
            if on_progress is not None:
                part_progress = lambda seconds, before=done_seconds: on_progress(before + seconds)  # noqa: E731
            lines.extend(
                TranscriptLine(line.timestamp_seconds + offset_seconds, line.source, line.text)
                for line in self.transcribe(
                    audio_path, source=source, on_progress=part_progress, language=language, vocabulary=vocabulary
                )
            )
            done_seconds += audio_duration_seconds(audio_path)
            if on_progress is not None:
                on_progress(done_seconds)  # the silence after its last segment counts as done too
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
        next_start = start + audio_duration_seconds(audio_path)
    return offsets


def audio_duration_seconds(path: Path) -> float:
    """Length of a recording part in seconds — a WAV as recorded, or the Opus file it was compressed to
    once transcribed (see audio.archive) — or 0.0 if it can't be read: a missing or unreadable part
    shouldn't sink a transcript that otherwise came out fine."""
    if Path(path).suffix.lower() != ".wav":
        return _compressed_duration_seconds(path)
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wav_file:
            framerate = wav_file.getframerate()
            return wav_file.getnframes() / framerate if framerate else 0.0
    except (OSError, EOFError, wave.Error):  # EOFError: a 0-byte file, which never got a header
        return 0.0


def _compressed_duration_seconds(path: Path) -> float:
    """Duration of a compressed audio file, read by PyAV (which faster-whisper already depends on)."""
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration is not None:
                return container.duration / av.time_base
            stream = container.streams.audio[0]
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
    except Exception:  # av raises its own error types for a missing, empty or corrupt file
        return 0.0
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
