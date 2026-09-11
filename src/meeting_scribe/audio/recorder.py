"""Simultaneous microphone + system-audio capture.

Windows-only: system audio is captured via WASAPI loopback using PyAudioWPatch, a PyAudio fork that
exposes "record what's playing on the speakers" without a virtual audio cable. There is no portable
equivalent, which is why this whole app targets Windows.

This module can be *imported* on any platform (the pyaudiowpatch import is deferred to `start()`), but
`Recorder.start()` raises `RuntimeError` off Windows.
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CHUNK_FRAMES = 1024
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM

# A WAV file can't grow forever: RIFF stores every chunk size as a 32-bit integer, so the header stops
# being able to describe the file somewhere under 4 GiB — and Python's `wave` re-patches that header on
# every single write, so the moment a recording crosses the line, `writeframes` raises struct.error
# ("'L' format requires 0 <= number <= 4294967295") and takes the capture thread down with it. That used
# to end the recording silently: the thread is a daemon with nobody watching it, a windowed build has no
# console to print the traceback to, and what was left on disk was a file whose header no longer matched
# its contents, so transcription came back empty for the rest of the meeting. Long recordings now roll
# over into numbered part files instead (see _SegmentedWavWriter).
#
# The cap sits just under *2* GiB rather than 4: plenty of WAV readers (and audio libraries wrapping
# them) treat those unsigned sizes as signed and misread anything past 2 GiB. At 48 kHz stereo that's
# still ~3 hours per part.
MAX_WAV_DATA_BYTES = 2 * 1024**3 - 1024 * 1024

# Guards PyAudio() construction and .terminate() specifically — not stream open/read/close, which is
# already safely concurrent (that's the whole point of separate mic/system capture threads within one
# Recorder). Those two calls touch PortAudio's global init state and, on Windows, WASAPI's COM setup/
# teardown, which isn't necessarily safe to interleave across *different* Recorder instances. Back-to-back
# meetings (see session.py) can now have one meeting's Recorder.stop() tearing down on a background
# thread while a new meeting's Recorder.start() is initializing on the GUI thread; without this lock that
# race could crash the whole process natively — silently, since a native crash bypasses Python's
# exception handling (and a windowed build has no console to show a traceback on even if it didn't).
_pyaudio_lifecycle_lock = threading.Lock()


# A plain linear ratio (rms / full-scale) makes a normal-volume microphone barely move the meter: typical
# speech sits maybe 1-5% of full scale, which renders as a sliver nobody can see, while system audio
# (already mixed/normalized by the OS) tends to run much hotter and dominates the same linear scale. A
# dBFS floor maps that same signal across most of the visible range instead, the way a real VU meter
# does — anything at or below this floor reads as silence (0.0), anything at full scale reads as 1.0.
_SILENCE_FLOOR_DB = -60.0

# Stands in for 20*log10(0) on a chunk of pure digital silence, which is negative infinity — a finite
# floor keeps the arithmetic (and the formatting) downstream from having to special-case -inf.
_SILENT_DBFS = -120.0

# Above this, the device is hearing *something* — not necessarily speech. That's deliberate: the question
# these checks answer is "is this input picking the room up at all", and a keyboard, a cough or a chair
# all answer it just as well as a sentence does. A real microphone in a quiet room idles around -60 to
# -50 dBFS; normal speech lands around -30 to -20. -40 sits in the gap.
_ACTIVITY_DBFS = -40.0

# A sample this close to the rail is a clipped sample. One is nothing (a door slam peaks too); a chunk
# where half a percent of samples are pinned there is a signal that's genuinely too hot, and several
# such chunks means it's the input level rather than a one-off.
_CLIPPING_SAMPLE = 32700
_CLIPPED_FRACTION = 0.005
_CLIPPED_CHUNKS_BEFORE_WARNING = 3

# How long after the last clipped chunk the clipping warning stays up.
_CLIP_HOLD_SECONDS = 5.0

# How long a stream has to have been running before its silence means anything. Digital silence is
# damning almost immediately — a working device never produces it — but "nothing above speech level"
# needs a long fuse, because that's also what the first minute of a meeting nobody has spoken in yet
# looks like.
_DIGITAL_SILENCE_GRACE_SECONDS = 15.0
_NO_ACTIVITY_GRACE_SECONDS = 90.0


@dataclass(frozen=True)
class _ChunkStats:
    """What one chunk of PCM tells us about the device that produced it."""

    level: float  # 0..1, for the meter
    rms_dbfs: float
    all_zero: bool  # every sample exactly 0 — see StreamHealth.digital_silence
    clipped: bool


def _analyze_pcm16(data: bytes) -> _ChunkStats:
    """Measures one 16-bit PCM chunk. Everything the level meter and the input checks below need comes
    from here, so a capture thread pays for the analysis once per chunk rather than once per question."""
    samples = np.frombuffer(data, dtype=np.int16) if data else np.empty(0, dtype=np.int16)
    if samples.size == 0:
        return _ChunkStats(level=0.0, rms_dbfs=_SILENT_DBFS, all_zero=True, clipped=False)
    # int16 can't hold abs(-32768), so widen before taking magnitudes.
    magnitudes = np.abs(samples.astype(np.int32))
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
    rms_dbfs = 20.0 * float(np.log10(rms / 32768.0)) if rms > 0 else _SILENT_DBFS
    level = float(np.clip((rms_dbfs - _SILENCE_FLOOR_DB) / -_SILENCE_FLOOR_DB, 0.0, 1.0))
    clipped_fraction = float(np.count_nonzero(magnitudes >= _CLIPPING_SAMPLE)) / magnitudes.size
    return _ChunkStats(
        level=level,
        rms_dbfs=rms_dbfs,
        all_zero=bool(magnitudes.max() == 0),
        clipped=clipped_fraction > _CLIPPED_FRACTION,
    )


def _pcm16_level(data: bytes) -> float:
    """RMS level of a 16-bit PCM chunk on a logarithmic (dBFS) scale, normalized to 0..1 for a VU-meter
    display. Not calibrated to any broadcast standard — it only needs to make normal speaking volume
    visibly move the meter and sit near zero with silence, so a user can tell at a glance whether a
    device is actually picking anything up."""
    return _analyze_pcm16(data).level


@dataclass(frozen=True)
class StreamHealth:
    """A snapshot of what one capture stream has actually been hearing.

    The level meters answer "how loud is it right now"; this answers the question a meter can't, which
    is whether the device has ever picked anything up at all. `digital_silence` is the strongest signal
    of the lot: a working microphone in a silent room still produces a noise floor, so a stream of
    literally all-zero samples means nothing is reaching the app — the wrong input, a disconnected jack,
    or a device muted below the point where Windows still hands it to us.
    """

    seconds_captured: float
    peak_dbfs: float  # loudest chunk since the stream started
    heard_activity: bool
    seconds_since_activity: float | None  # None if it has never heard anything
    digital_silence: bool
    clipping: bool

    @classmethod
    def idle(cls) -> StreamHealth:
        """A stream that hasn't produced a single chunk yet — not silent, just not started. Nothing can
        be concluded from it, so every warning-triggering field reads false."""
        return cls(
            seconds_captured=0.0,
            peak_dbfs=_SILENT_DBFS,
            heard_activity=False,
            seconds_since_activity=None,
            digital_silence=False,
            clipping=False,
        )


class _StreamActivityMonitor:
    """Running tally of what a capture stream is hearing, fed one chunk at a time.

    Deliberately holds no history: a handful of timestamps and counters answer every question the
    warnings ask, so this costs the capture thread a lock and a few comparisons per chunk rather than a
    buffer that grows with the meeting. The lock is what makes `health()` safe to poll from the GUI
    thread while a capture thread is writing.
    """

    def __init__(self, activity_dbfs: float = _ACTIVITY_DBFS):
        self._activity_dbfs = activity_dbfs
        self._lock = threading.Lock()
        self._first_observed_at: float | None = None
        self._last_observed_at: float | None = None
        self._last_activity_at: float | None = None
        self._last_nonzero_at: float | None = None
        self._last_clip_at: float | None = None
        self._clipped_chunks = 0
        self._peak_dbfs = _SILENT_DBFS

    def observe(self, stats: _ChunkStats, now: float) -> None:
        with self._lock:
            if self._first_observed_at is None:
                self._first_observed_at = now
            self._last_observed_at = now
            self._peak_dbfs = max(self._peak_dbfs, stats.rms_dbfs)
            if not stats.all_zero:
                self._last_nonzero_at = now
            if stats.rms_dbfs >= self._activity_dbfs:
                self._last_activity_at = now
            if stats.clipped:
                self._clipped_chunks += 1
                self._last_clip_at = now

    def health(self, now: float) -> StreamHealth:
        with self._lock:
            if self._first_observed_at is None:
                return StreamHealth.idle()
            seconds_captured = max(0.0, now - self._first_observed_at)
            return StreamHealth(
                seconds_captured=seconds_captured,
                peak_dbfs=self._peak_dbfs,
                heard_activity=self._last_activity_at is not None,
                seconds_since_activity=(
                    None if self._last_activity_at is None else now - self._last_activity_at
                ),
                digital_silence=self._last_nonzero_at is None,
                clipping=(
                    self._clipped_chunks >= _CLIPPED_CHUNKS_BEFORE_WARNING
                    and self._last_clip_at is not None
                    and now - self._last_clip_at <= _CLIP_HOLD_SECONDS
                ),
            )


def _describe_stream_problem(health: StreamHealth, label: str, other_heard_activity: bool) -> str | None:
    """The one thing worth saying about a stream right now, or None if it looks fine.

    Ordered by how certain each conclusion is. Digital silence is a statement of fact about the device.
    "Heard nothing while the other side was busy" is a genuine heuristic — it can't tell a wrong device
    from a muted one from a meeting nobody has spoken in — which is why it needs the other stream to
    have been active before it says anything, and why it's phrased as something to check rather than a
    diagnosis.
    """
    if health.digital_silence and health.seconds_captured >= _DIGITAL_SILENCE_GRACE_SECONDS:
        return (
            f"{label}: no signal at all — every sample is digital silence. A working device always has "
            "some noise floor, so this is almost certainly the wrong input, an unplugged one, or one "
            "muted at the driver."
        )
    if (
        not health.heard_activity
        and health.seconds_captured >= _NO_ACTIVITY_GRACE_SECONDS
        and other_heard_activity
    ):
        return (
            f"{label}: nothing above speech level in "
            f"{int(health.seconds_captured)}s (peak {health.peak_dbfs:.0f} dBFS) while the other track "
            "was active — check the right device is selected and that it isn't muted."
        )
    if health.clipping:
        return (
            f"{label}: clipping (peak {health.peak_dbfs:.0f} dBFS). Turn the input level down in "
            "Windows, or the recording will distort."
        )
    return None


def describe_input_problems(mic: StreamHealth, system: StreamHealth) -> tuple[str, ...]:
    """Everything worth warning about across both capture streams, as ready-to-show sentences.

    A pure function of two snapshots, so the decision of what counts as a problem is testable on its
    own, without devices, threads or a GUI. Each stream is judged partly by the other: a silent
    microphone means something quite different depending on whether anyone else on the call was
    audible."""
    problems = (
        _describe_stream_problem(mic, "Microphone", system.heard_activity),
        _describe_stream_problem(system, "System audio", mic.heard_activity),
    )
    return tuple(problem for problem in problems if problem is not None)


@dataclass(frozen=True)
class InputCheck:
    """The result of listening to a device for a few seconds before a meeting starts (see
    check_input_device) — the moment when "that's the wrong microphone" is both unambiguous and one
    dropdown away from being fixed."""

    device_name: str
    requested_name: str | None
    peak_dbfs: float
    heard_activity: bool
    digital_silence: bool
    clipping: bool

    @property
    def substituted_device(self) -> bool:
        """True when the device that was asked for isn't the one that got tested, because Windows no
        longer has it — the same silent substitution that happens at the start of a real recording."""
        return self.requested_name is not None and self.requested_name != self.device_name

    @property
    def is_ok(self) -> bool:
        return self.heard_activity and not self.clipping and not self.substituted_device

    @property
    def summary(self) -> str:
        prefix = ""
        if self.substituted_device:
            prefix = (
                f"{self.requested_name!r} isn't available right now, so this tested {self.device_name!r} "
                "instead — which is also what a recording would use. "
            )
        if self.digital_silence:
            return (
                f"{prefix}No signal at all from {self.device_name!r} — every sample was digital silence. "
                "That's the wrong device, an unplugged one, or one muted at the driver."
            )
        if self.clipping:
            return (
                f"{prefix}{self.device_name!r} is clipping (peak {self.peak_dbfs:.0f} dBFS). Turn its "
                "input level down in Windows."
            )
        if not self.heard_activity:
            return (
                f"{prefix}{self.device_name!r} is producing signal, but nothing above speech level "
                f"(peak {self.peak_dbfs:.0f} dBFS). Say something while the test runs — if that's the "
                "best it does, it's picking up a room rather than you, or it's the wrong device."
            )
        return f"{prefix}{self.device_name!r} sounds fine — peak {self.peak_dbfs:.0f} dBFS."


def _find_input_device(pyaudio_instance, name: str | None) -> dict | None:
    """The input device Windows currently calls `name`, or None if nothing answers to it anymore —
    device names come and go with docks, USB headsets and driver updates."""
    if not name:
        return None
    for index in range(pyaudio_instance.get_device_count()):
        info = pyaudio_instance.get_device_info_by_index(index)
        if info.get("name") == name and info.get("maxInputChannels", 0) > 0:
            return info
    return None


def check_input_device(device_name: str | None = None, seconds: float = 3.0) -> InputCheck:
    """Records a few seconds from a microphone and reports what it heard, without starting a meeting.

    This is the version of the warning that's actually actionable: during a meeting, a silent
    microphone is ambiguous (wrong device? muted? nobody talking?), but during a test the user knows
    they're supposed to be making noise, so silence means something is wrong with the input."""
    if sys.platform != "win32":
        raise RuntimeError(
            "Testing an input device requires Windows (PyAudioWPatch); "
            f"unsupported platform: {sys.platform!r}"
        )
    import pyaudiowpatch as pyaudio

    with _pyaudio_lifecycle_lock:
        pyaudio_instance = pyaudio.PyAudio()
    try:
        device_info = _find_input_device(pyaudio_instance, device_name)
        if device_info is None:
            device_info = pyaudio_instance.get_default_input_device_info()
        monitor = _StreamActivityMonitor()
        stream = pyaudio_instance.open(
            format=pyaudio.paInt16,
            channels=int(device_info["maxInputChannels"]),
            rate=int(device_info["defaultSampleRate"]),
            input=True,
            input_device_index=device_info["index"],
            frames_per_buffer=CHUNK_FRAMES,
        )
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                monitor.observe(_analyze_pcm16(data), time.monotonic())
        finally:
            stream.stop_stream()
            stream.close()
        health = monitor.health(time.monotonic())
    finally:
        with _pyaudio_lifecycle_lock:
            pyaudio_instance.terminate()

    return InputCheck(
        device_name=str(device_info["name"]),
        requested_name=device_name,
        peak_dbfs=health.peak_dbfs,
        heard_activity=health.heard_activity,
        digital_silence=health.digital_silence,
        clipping=health.clipping,
    )


class _SegmentedWavWriter:
    """Writes one capture stream to a series of WAV files, rolling over to a fresh part before the
    current one grows past what a RIFF header can describe (see MAX_WAV_DATA_BYTES).

    The first part keeps the plain name it was given ("mic.wav"), so an ordinary meeting produces
    exactly the same single file it always did; only a recording long enough to overflow ever grows a
    "mic.part2.wav" beside it. `paths` lists the parts written so far, in order.
    """

    def __init__(
        self,
        base_path: Path,
        channels: int,
        sample_width: int,
        framerate: int,
        max_data_bytes: int = MAX_WAV_DATA_BYTES,
    ):
        self._base_path = Path(base_path)
        self._channels = channels
        self._sample_width = sample_width
        self._framerate = framerate
        self._max_data_bytes = max(int(max_data_bytes), 1)
        self._paths: list[Path] = []
        self._wav_file: wave.Wave_write | None = None
        self._bytes_in_part = 0
        self._open_next_part()

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self._paths)

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self._wav_file is None:
            raise ValueError("writer is already closed")
        # The `self._bytes_in_part` check keeps a chunk that's somehow bigger than the cap from rolling
        # over forever into empty parts — it goes into a part of its own instead.
        if self._bytes_in_part and self._bytes_in_part + len(data) > self._max_data_bytes:
            self._close_current_part()
            self._open_next_part()
        self._wav_file.writeframes(data)
        self._bytes_in_part += len(data)

    def close(self) -> None:
        self._close_current_part()

    def _part_path(self, number: int) -> Path:
        if number == 1:
            return self._base_path
        return self._base_path.with_name(
            f"{self._base_path.stem}.part{number}{self._base_path.suffix}"
        )

    def _open_next_part(self) -> None:
        path = self._part_path(len(self._paths) + 1)
        wav_file = wave.open(str(path), "wb")
        wav_file.setnchannels(self._channels)
        wav_file.setsampwidth(self._sample_width)
        wav_file.setframerate(self._framerate)
        self._wav_file = wav_file
        self._bytes_in_part = 0
        self._paths.append(path)

    def _close_current_part(self) -> None:
        wav_file, self._wav_file = self._wav_file, None
        self._bytes_in_part = 0
        if wav_file is not None:
            wav_file.close()


@dataclass(frozen=True)
class RecordedAudio:
    """`mic_paths` / `system_paths` are each one capture stream in chronological order — normally a
    single file, but a meeting long enough to overflow a WAV header spills into further parts (see
    _SegmentedWavWriter), which transcription stitches back onto one clock.

    `notices` is everything the recorder wants the user to know about this recording, as ready-to-show
    sentences: a capture thread that died mid-meeting, a selected device Windows no longer has, an input
    that never picked anything up. A half-recorded meeting says so instead of just coming back quiet."""

    mic_paths: tuple[Path, ...]
    system_paths: tuple[Path, ...]
    started_at_monotonic: float
    notices: tuple[str, ...] = ()


class Recorder:
    """Records a microphone and an output device's WASAPI loopback to two WAV files.

    The two streams are kept in separate files (rather than mixed) so the transcription engine can
    label segments by source ("mic" vs. "system") when it merges them into the meeting transcript.

    `mic_device_name` / `system_device_name` select a specific device by name (see
    audio.device_picker), looked up fresh at `start()` so a device unplugged since it was chosen falls
    back to the current Windows default rather than failing. Leave either as None to always use
    whatever Windows currently considers the default — the original, simpler behavior.

    While running, `mic_level` and `system_level` hold each stream's current input level (roughly 0..1,
    see `_pcm16_level`), updated every chunk by the capture threads — a GUI can poll these to show a
    live "is this actually picking up audio" meter without needing any thread synchronization beyond
    reading a plain float, which is safe under the GIL.

    A capture thread that dies (device unplugged, disk full, a driver error mid-read) doesn't take the
    meeting with it — the other stream keeps recording — but it does record why, in `capture_notices()`,
    which the GUI polls alongside the levels so a dead microphone is visible while the meeting is still
    running rather than a surprise in an empty transcript afterwards. The same channel reports a selected
    device Windows no longer has, which is otherwise substituted silently.

    `mic_health()` / `system_health()` and `input_problems()` go a step further and say whether what's
    being captured looks like a working input at all — see StreamHealth and describe_input_problems.
    """

    def __init__(
        self,
        output_dir: Path,
        mic_device_name: str | None = None,
        system_device_name: str | None = None,
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._mic_device_name = mic_device_name
        self._system_device_name = system_device_name
        self._pyaudio = None
        self._mic_thread: threading.Thread | None = None
        self._system_thread: threading.Thread | None = None
        self._mic_writer: _SegmentedWavWriter | None = None
        self._system_writer: _SegmentedWavWriter | None = None
        self._mic_monitor = _StreamActivityMonitor()
        self._system_monitor = _StreamActivityMonitor()
        self._stop_event = threading.Event()
        self._started_at: float | None = None
        self._mic_path = self._output_dir / "mic.wav"
        self._system_path = self._output_dir / "system.wav"
        self._notices: list[str] = []
        self._notices_lock = threading.Lock()
        self.mic_level = 0.0
        self.system_level = 0.0

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "Audio recording requires Windows (WASAPI loopback via PyAudioWPatch); "
                f"unsupported platform: {sys.platform!r}"
            )
        import pyaudiowpatch as pyaudio

        with _pyaudio_lifecycle_lock:
            self._pyaudio = pyaudio.PyAudio()
        self._stop_event.clear()
        self._started_at = time.monotonic()

        mic_info = self._resolve_input_device(self._mic_device_name)
        system_info = self._resolve_loopback_device(pyaudio, self._system_device_name)

        self._mic_writer, self._mic_thread = self._spawn_capture_thread(
            pyaudio, mic_info, self._mic_path, "mic_level", self._mic_monitor, "Microphone"
        )
        self._system_writer, self._system_thread = self._spawn_capture_thread(
            pyaudio, system_info, self._system_path, "system_level", self._system_monitor, "System audio"
        )

    def stop(self) -> RecordedAudio:
        if self._started_at is None:
            raise RuntimeError("Recorder.start() was never called")
        self._stop_event.set()
        for thread in (self._mic_thread, self._system_thread):
            if thread is not None:
                thread.join(timeout=5)
        if self._pyaudio is not None:
            with _pyaudio_lifecycle_lock:
                self._pyaudio.terminate()
        # The GUI may already have flagged these live; recording them here too is what puts them in
        # front of whoever reads the finished meeting, who wasn't necessarily watching at the time.
        for problem in self.input_problems():
            self._record_notice(problem)
        return RecordedAudio(
            mic_paths=self._written_paths(self._mic_writer, self._mic_path),
            system_paths=self._written_paths(self._system_writer, self._system_path),
            started_at_monotonic=self._started_at,
            notices=self.capture_notices(),
        )

    def capture_notices(self) -> tuple[str, ...]:
        """Everything the recorder has to report about this recording so far, newest last — a dead
        capture thread, a device that had to be substituted. Empty while nothing is wrong. Safe to poll
        from another thread at any time, including mid-recording."""
        with self._notices_lock:
            return tuple(self._notices)

    def mic_health(self) -> StreamHealth:
        return self._mic_monitor.health(time.monotonic())

    def system_health(self) -> StreamHealth:
        return self._system_monitor.health(time.monotonic())

    def input_problems(self) -> tuple[str, ...]:
        """What currently looks wrong with either input, as ready-to-show sentences — empty while both
        are behaving. Recomputed on every call rather than latched, so a microphone that comes back to
        life (the user unmutes it, or picks the right device next meeting) stops being reported."""
        return describe_input_problems(self.mic_health(), self.system_health())

    @staticmethod
    def _written_paths(writer: _SegmentedWavWriter | None, fallback: Path) -> tuple[Path, ...]:
        # The fallback covers a stream whose thread never got as far as opening a file at all; the empty
        # WAV it names may not exist, which transcription already tolerates.
        return writer.paths if writer is not None else (fallback,)

    def _record_notice(self, message: str) -> None:
        with self._notices_lock:
            if message not in self._notices:
                self._notices.append(message)

    def _record_capture_failure(self, label: str, exc: BaseException) -> None:
        with self._notices_lock:
            # Not deduplicated the way _record_notice is: two identical failures on the two streams are
            # two separate things that went wrong, and both are worth saying.
            self._notices.append(f"{label} capture stopped early: {type(exc).__name__}: {exc}")

    def _resolve_input_device(self, name: str | None) -> dict:
        chosen = _find_input_device(self._pyaudio, name)
        if chosen is not None:
            return chosen
        # Falling back to the Windows default is the right behavior — a meeting shouldn't fail to record
        # because a headset was left at home — but doing it silently is how someone ends up with an hour
        # of a laptop's array microphone when they thought they were on a headset. This is the one
        # "wrong input device" case that needs no guesswork at all to detect, so it's stated outright.
        fallback = self._pyaudio.get_default_input_device_info()
        if name:
            self._record_notice(
                f"Microphone {name!r} isn't available — recording from {fallback['name']!r} "
                "(the Windows default) instead."
            )
        return fallback

    def _resolve_loopback_device(self, pyaudio_module, name: str | None) -> dict:
        if name:
            for loopback in self._pyaudio.get_loopback_device_info_generator():
                if loopback.get("name") == name:
                    return loopback
        fallback = self._get_default_loopback_device(pyaudio_module)
        if name:
            self._record_notice(
                f"System audio device {name!r} isn't available — recording from {fallback['name']!r} "
                "(the Windows default) instead."
            )
        return fallback

    def _get_default_loopback_device(self, pyaudio_module) -> dict:
        wasapi_info = self._pyaudio.get_host_api_info_by_type(pyaudio_module.paWASAPI)
        default_speakers = self._pyaudio.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if default_speakers.get("isLoopbackDevice"):
            return default_speakers
        for loopback in self._pyaudio.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                return loopback
        raise RuntimeError(
            f"No WASAPI loopback device found for default output {default_speakers['name']!r}. "
            "System-audio capture needs a WASAPI-capable output device."
        )

    def _spawn_capture_thread(
        self,
        pyaudio_module,
        device_info: dict,
        path: Path,
        level_attr: str,
        monitor: _StreamActivityMonitor,
        label: str,
    ) -> tuple[_SegmentedWavWriter, threading.Thread]:
        channels = int(device_info["maxInputChannels"])
        rate = int(device_info["defaultSampleRate"])

        writer = _SegmentedWavWriter(path, channels, SAMPLE_WIDTH_BYTES, rate)

        def run() -> None:
            stream = None
            try:
                stream = self._pyaudio.open(
                    format=pyaudio_module.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=device_info["index"],
                    frames_per_buffer=CHUNK_FRAMES,
                )
                while not self._stop_event.is_set():
                    data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                    writer.write(data)
                    stats = _analyze_pcm16(data)
                    setattr(self, level_attr, stats.level)
                    monitor.observe(stats, time.monotonic())
            except Exception as exc:
                # Nothing is watching this thread, so an escaping exception would be invisible; record
                # it for the GUI and for RecordedAudio.notices, and still close everything below.
                self._record_capture_failure(label, exc)
            finally:
                for close in (
                    getattr(stream, "stop_stream", None),
                    getattr(stream, "close", None),
                    writer.close,
                ):
                    try:
                        if close is not None:
                            close()
                    except Exception as exc:  # one failed teardown step shouldn't skip the rest
                        self._record_capture_failure(label, exc)
                setattr(self, level_attr, 0.0)

        thread = threading.Thread(target=run, daemon=True, name=f"recorder-{path.stem}")
        thread.start()
        return writer, thread
