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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np

from meeting_scribe.audio.call_devices import CallAudioDevices, find_call_audio_devices, match_portaudio_device
from meeting_scribe.audio.device_watch import device_signature

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
# How long an open stream can go without delivering a single chunk before that's worth saying. Shorter
# than the no-activity fuse: a quiet meeting still delivers chunks (low-level ones), so receiving nothing
# at all isn't a quiet meeting — for system audio it means nothing is playing through that output device.
_NO_DATA_GRACE_SECONDS = 30.0

# Automatic device selection (see Recorder's auto_* arguments). A chunk louder than _AUDIBLE_DBFS is
# carrying sound someone could hear; a quiet stream never gets there. An output device another one is
# switched to has to be clearly playing, not merely above that line — a switch is a guess about where
# the call is, and it should only be made on good evidence.
_AUDIBLE_DBFS = -60.0
_PLAYING_DBFS = -50.0
# How often the automation looks at the two streams, and how long it listens to a device it's
# considering. A probe opens its own stream beside the one recording, so recording never pauses for it.
_AUTO_DEVICE_POLL_SECONDS = 2.0
_PROBE_SECONDS = 1.0
# Nothing audible through the recorded output for this long means the call is probably coming out of a
# different one. Long enough to ride out an ordinary pause in a conversation.
_SYSTEM_QUIET_BEFORE_SEARCH_SECONDS = 8.0
# How often, at most, the other outputs are listened to while the recorded one stays quiet.
_SYSTEM_SEARCH_INTERVAL_SECONDS = 4.0
# A connected headset whose microphone hands over nothing but digital silence for this long is muted at
# the headset or switched off, so the laptop's microphone takes over.
_HEADSET_SILENT_BEFORE_FALLBACK_SECONDS = 10.0
# While on the laptop microphone, how often the headset is checked for having come back to life.
_HEADSET_RECHECK_SECONDS = 15.0
# How a headset microphone is recognized by name when Settings doesn't name one. Windows' own names for
# them ("Headset Microphone (…)", "Headset (… Hands-Free)") nearly always say so.
_HEADSET_NAME_HINTS = ("headset", "headphone", "hands-free", "handsfree", "earbud", "earphone", "airpods")
# Inputs that are really the computer's output looped back — never a sensible microphone fallback.
_NOT_A_MICROPHONE_HINTS = ("stereo mix", "what u hear", "wave out", "loopback")
# How long one answer to "which devices is Teams using" is reused. The microphone and system-audio checks
# both ask on each pass of the automation, a moment apart; asking Windows twice for the same thing buys
# nothing.
_CALL_DEVICES_CACHE_SECONDS = 1.0

# WASAPI loopback delivers nothing at all while nothing plays through the output device — not silence,
# no data. Written as-is, the system-audio file would only hold the moments something was playing, run
# shorter than the meeting, and drift further from the microphone track with every pause, so everything
# after the first quiet stretch would be transcribed at the wrong time. The capture thread fills those
# stretches with silence instead, keeping the file on the wall clock. It only fills once nothing has
# arrived for this long — a stream that's playing hands over data every few milliseconds — so it never
# lands in the middle of a sentence.
_LOOPBACK_GAP_SECONDS = 0.25
# Once filling, the thread catches up every few milliseconds, so it's never this far behind unless it
# wasn't running at all — the computer went to sleep mid-meeting.
_MAX_LOOPBACK_FILL_SECONDS = 5.0

# A capture stream that dies mid-meeting (a Bluetooth headset switching to its hands-free profile when a
# call starts, a driver hiccup, a device reset) is reopened rather than left dead for the rest of the
# meeting. Retries back off, so a device that fails straight away every time isn't hammered; a stream
# that then runs this long has recovered, and the next failure starts the back-off again.
_RECOVERY_FIRST_DELAY_SECONDS = 1.0
_RECOVERY_MAX_DELAY_SECONDS = 60.0
_RECOVERY_RESET_AFTER_SECONDS = 120.0


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
    # How long the stream has been open without delivering a single chunk — None once anything has
    # arrived since it (last) opened. WASAPI loopback only delivers while something is playing through
    # that output device, so a system-audio stream that stays at this for long means the call is coming
    # out of a different device than the one being recorded (and its thread sits blocked in read()).
    seconds_without_data: float | None = None
    # How long since the stream last carried audible sound (a chunk above _AUDIBLE_DBFS) / any signal at
    # all (a non-zero sample) — counted from when it (last) opened if it hasn't since, and None if it
    # never opened. What automatic device selection goes on: a quiet output device, a dead headset.
    seconds_without_sound: float | None = None
    seconds_without_signal: float | None = None

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
        self._last_audible_at: float | None = None
        self._last_clip_at: float | None = None
        self._clipped_chunks = 0
        self._peak_dbfs = _SILENT_DBFS
        self._opened_at: float | None = None

    def stream_opened(self, now: float) -> None:
        """Called each time a capture stream opens — at the start of a meeting and after a device switch —
        so a stream that never delivers anything can be told apart from one that hasn't started yet."""
        with self._lock:
            self._opened_at = now

    def observe(self, stats: _ChunkStats, now: float) -> None:
        with self._lock:
            if self._first_observed_at is None:
                self._first_observed_at = now
            self._last_observed_at = now
            self._peak_dbfs = max(self._peak_dbfs, stats.rms_dbfs)
            if not stats.all_zero:
                self._last_nonzero_at = now
            if stats.rms_dbfs >= _AUDIBLE_DBFS:
                self._last_audible_at = now
            if stats.rms_dbfs >= self._activity_dbfs:
                self._last_activity_at = now
            if stats.clipped:
                self._clipped_chunks += 1
                self._last_clip_at = now

    def health(self, now: float) -> StreamHealth:
        with self._lock:
            waiting = None
            if self._opened_at is not None and (
                self._last_observed_at is None or self._last_observed_at < self._opened_at
            ):
                waiting = now - self._opened_at
            without_sound = self._seconds_since(self._last_audible_at, now)
            without_signal = self._seconds_since(self._last_nonzero_at, now)
            if self._first_observed_at is None:
                return replace(
                    StreamHealth.idle(),
                    seconds_without_data=waiting,
                    seconds_without_sound=without_sound,
                    seconds_without_signal=without_signal,
                )
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
                seconds_without_data=waiting,
                seconds_without_sound=without_sound,
                seconds_without_signal=without_signal,
            )

    def _seconds_since(self, last_at: float | None, now: float) -> float | None:
        """Time since `last_at`, or since the stream opened if that's later — what happened on the
        device before a switch says nothing about the one now open. Caller holds the lock."""
        if self._opened_at is None:
            return None
        return now - max(self._opened_at, last_at if last_at is not None else self._opened_at)


def _describe_stream_problem(health: StreamHealth, label: str, other_heard_activity: bool) -> str | None:
    """The one thing worth saying about a stream right now, or None if it looks fine.

    Ordered by how certain each conclusion is. Digital silence is a statement of fact about the device.
    "Heard nothing while the other side was busy" is a genuine heuristic — it can't tell a wrong device
    from a muted one from a meeting nobody has spoken in — which is why it needs the other stream to
    have been active before it says anything, and why it's phrased as something to check rather than a
    diagnosis.
    """
    if (
        health.seconds_without_data is not None
        and health.seconds_without_data >= _NO_DATA_GRACE_SECONDS
        and other_heard_activity
    ):
        return (
            f"{label}: nothing at all has come through in {int(health.seconds_without_data)}s while the "
            "other track was active. Windows only passes on audio from the selected device, so a call "
            "playing through a different one (a headset, say) isn't being recorded — check the right "
            "device is selected."
        )
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


def _read_what_is_available(stream) -> bytes:
    """Reads whatever `stream` already has buffered, without waiting for more — b"" when it has nothing.
    A blocking read() on a WASAPI loopback stream waits for as long as nothing plays through that output
    device, which can be the whole meeting; this never does, so whoever is reading can still notice it's
    been asked to stop."""
    available = stream.get_read_available()
    if available <= 0:
        return b""
    return stream.read(min(available, CHUNK_FRAMES), exception_on_overflow=False)


@dataclass(frozen=True)
class DeviceProbe:
    """What a short listen to one device heard — see _probe_devices."""

    name: str
    peak_dbfs: float
    heard_signal: bool  # any non-zero sample at all: the device is live, whether or not it's loud


class _ProbeStream:
    def __init__(self, name: str, stream):
        self.name = name
        self.stream = stream
        self.peak_dbfs = _SILENT_DBFS
        self.heard_signal = False


def _probe_devices(
    pyaudio_instance, pyaudio_module, devices, seconds: float, stop_event: threading.Event
) -> list[DeviceProbe]:
    """Listens to every device in `devices` at once for `seconds`, each on a stream of its own opened
    beside whatever is recording, and reports what each heard. Reads only what's buffered (see
    _read_what_is_available), so a silent loopback device can't hang it, and it returns early once
    `stop_event` is set. A device that won't open, or fails mid-listen, is reported as having heard
    nothing rather than failing the rest."""
    probes: list[_ProbeStream] = []
    try:
        for info in devices:
            try:
                stream = pyaudio_instance.open(
                    format=pyaudio_module.paInt16,
                    channels=int(info["maxInputChannels"]),
                    rate=int(info["defaultSampleRate"]),
                    input=True,
                    input_device_index=info["index"],
                    frames_per_buffer=CHUNK_FRAMES,
                )
            except Exception:
                stream = None
            probes.append(_ProbeStream(str(info["name"]), stream))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not stop_event.is_set():
            for probe in probes:
                if probe.stream is None:
                    continue
                try:
                    data = _read_what_is_available(probe.stream)
                except Exception:
                    data = b""
                if data:
                    stats = _analyze_pcm16(data)
                    probe.peak_dbfs = max(probe.peak_dbfs, stats.rms_dbfs)
                    probe.heard_signal = probe.heard_signal or not stats.all_zero
            stop_event.wait(0.02)
    finally:
        for probe in probes:
            for close in (getattr(probe.stream, "stop_stream", None), getattr(probe.stream, "close", None)):
                try:
                    if close is not None:
                        close()
                except Exception:
                    pass
    return [DeviceProbe(probe.name, probe.peak_dbfs, probe.heard_signal) for probe in probes]


def looks_like_headset(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _HEADSET_NAME_HINTS)


def _same_device(a: str, b: str) -> bool:
    """Windows lists each device once per audio API, and the oldest (MME) cuts names off at 31
    characters — "Headset Microphone (Jabra Evol" is the same headset as the full name."""
    shorter, longer = sorted((a, b), key=len)
    return a == b or (len(shorter) >= 16 and longer.startswith(shorter))


def find_headset_microphones(input_names, configured: str | None) -> list[str]:
    """Every headset microphone connected right now, in order of preference: the one picked in Settings
    (`configured`) first, then each input whose name says it's a headset (see looks_like_headset). The
    same headset listed under several of Windows' audio APIs (see _same_device) counts once."""
    candidates = [name for name in input_names if name == configured or looks_like_headset(name)]
    candidates.sort(key=lambda name: name != configured)  # stable: otherwise keeps Windows' order
    headsets: list[str] = []
    for name in candidates:
        if not any(_same_device(name, kept) for kept in headsets):
            headsets.append(name)
    return headsets


def choose_fallback_microphone(
    input_names, *, configured: str | None, default_name: str | None, headsets
) -> str | None:
    """The microphone to use when no headset can be: the one chosen in Settings, else the Windows
    default, else the first input that isn't a headset — never a headset (Windows tends to make a newly
    connected one its default) or a loopback input like Stereo Mix."""

    def usable(name: str | None) -> bool:
        return (
            bool(name)
            and name in input_names
            and not any(_same_device(name, headset) for headset in headsets)
            and not looks_like_headset(name)
            and not any(hint in name.lower() for hint in _NOT_A_MICROPHONE_HINTS)
        )

    for candidate in (configured, default_name, *input_names):
        if usable(candidate):
            return candidate
    return None


def next_microphone(
    *,
    active: str | None,
    headsets,
    fallback: str | None,
    active_is_dead: bool,
    live_headsets,
) -> str | None:
    """Which microphone to switch to now, or None to stay put.

    `headsets` is every connected headset, most preferred first (see find_headset_microphones), and
    `live_headsets` the ones a probe just heard any signal from, in the same order — None if none were
    listened to this time. `active_is_dead` says the headset being recorded has handed over nothing but
    digital silence (muted at the headset, or switched off with its dongle still plugged in) — silence a
    working microphone never produces, so it's safe to act on.

    On a headset that's still working: stay. On one that's gone dead: the first other headset that's
    live, else the fallback. On the fallback: the first headset that's live."""
    live = [
        name for name in (live_headsets or ())
        if active is None or not _same_device(name, active)
    ]
    on_headset = active is not None and any(_same_device(active, headset) for headset in headsets)
    if not on_headset:
        return live[0] if live else None
    if not active_is_dead:
        return None
    if live:
        return live[0]
    return fallback


def next_system_device(
    *, active: str | None, active_seconds_without_sound: float | None, probes
) -> str | None:
    """Which output device to record instead, or None to stay put. Only one device plays the call, so
    once the recorded one has been quiet for a while, whichever other output is clearly playing
    something is the one to follow — the loudest, if more than one is."""
    if (
        active_seconds_without_sound is None
        or active_seconds_without_sound < _SYSTEM_QUIET_BEFORE_SEARCH_SECONDS
    ):
        return None
    playing = [probe for probe in probes if probe.name != active and probe.peak_dbfs >= _PLAYING_DBFS]
    if not playing:
        return None
    return max(playing, key=lambda probe: probe.peak_dbfs).name


class _SegmentedWavWriter:
    """Writes one capture stream to a series of WAV files, rolling over to a fresh part before the
    current one grows past what a RIFF header can describe (see MAX_WAV_DATA_BYTES).

    The first part keeps the plain name it was given ("mic.wav"), so an ordinary meeting produces
    exactly the same single file it always did; only a recording long enough to overflow ever grows a
    "mic.part2.wav" beside it. `paths` lists the parts written so far, in order.

    Locked internally because a device switch (Recorder.switch_mic_device/switch_system_device) hands the
    same writer to a freshly spawned capture thread while the old one is still shutting down — if the old
    thread's stream.read() is slow to notice its stop event, both threads could otherwise call write()/
    close() on the same underlying wave.Wave_write object at once, which isn't itself thread-safe.
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
        self._lock = threading.Lock()
        self._open_next_part()

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self._paths)

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._wav_file is None:
                raise ValueError("writer is already closed")
            # The `self._bytes_in_part` check keeps a chunk that's somehow bigger than the cap from
            # rolling over forever into empty parts — it goes into a part of its own instead.
            if self._bytes_in_part and self._bytes_in_part + len(data) > self._max_data_bytes:
                self._close_current_part()
                self._open_next_part()
            self._wav_file.writeframes(data)
            self._bytes_in_part += len(data)

    def close(self) -> None:
        with self._lock:
            self._close_current_part()

    def start_new_part(self, channels: int, framerate: int) -> None:
        """Forces a rollover into a fresh part right now, rather than waiting for the current one to
        grow past MAX_WAV_DATA_BYTES — for when the *device* being captured changed (see
        Recorder.switch_mic_device / switch_system_device), which can bring a different channel count
        or sample rate that the current part's WAV header was already committed to and can't change
        mid-file. The old part is closed with whatever it has so far; a new one starts with the new
        device's parameters."""
        with self._lock:
            self._close_current_part()
            self._channels = channels
            self._framerate = framerate
            self._open_next_part()

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


def discover_wav_parts(base_path: Path) -> tuple[Path, ...]:
    """The inverse of _SegmentedWavWriter's part naming: given "mic.wav", finds however many parts of
    that stream were actually written to disk ("mic.wav", "mic.part2.wav", ...) and returns them in
    order. Stops at the first missing number, which is safe because parts are always written
    sequentially with no gaps.

    This is what lets a stream be re-transcribed later from just its base path — no live
    _SegmentedWavWriter or Recorder needed — which retrying a meeting whose transcription failed (see
    session.retry_meeting_transcription) relies on."""
    parts: list[Path] = []
    number = 1
    while True:
        path = base_path if number == 1 else base_path.with_name(
            f"{base_path.stem}.part{number}{base_path.suffix}"
        )
        if not path.exists():
            return tuple(parts)
        parts.append(path)
        number += 1


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

    `switch_mic_device()` / `switch_system_device()` change the device a stream is reading from without
    stopping the meeting: the other stream keeps recording uninterrupted, and the switched stream rolls
    over into a new WAV part on the new device (see _SegmentedWavWriter.start_new_part) rather than
    silently continuing to read from whatever device happened to be open — which is what used to happen
    when a user changed their mic mid-meeting, producing a full-length recording of an idle device
    instead of an error.

    Devices plugged in, unplugged, or made the Windows default mid-meeting are a harder problem than
    they look: PortAudio snapshots the device list (defaults included) when it's initialized and never
    updates it while any instance is open — so for as long as this Recorder holds it, neither a new
    headset nor a new default is visible through it at all. A background thread — started in `start()`,
    stopped in `stop()` — polls a cheap winmm-based signature of the device set (see audio.device_watch)
    every `_DEVICE_POLL_SECONDS`, and when it changes calls `reload_devices()`: both capture threads are
    stopped, PortAudio is terminated and re-initialized to get a fresh list, and both streams are
    reopened by *name* (indices don't survive a re-init) into new WAV parts. A stream following "system
    default" (name None) lands on whatever the default is now; an explicitly chosen device that has
    disappeared falls back to the default with a notice, and is picked up again by name once it's back.
    The cost is a gap of a fraction of a second in both recordings, only when devices actually change.
    `device_list_version` is bumped on every reload so the GUI knows to re-read `available_devices()`.

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

    `auto_system_device` and `auto_microphone` hand the choice of device to the recorder, for someone
    who'd otherwise have to remember to change it for every call. A background thread checks every
    `_AUTO_DEVICE_POLL_SECONDS`: once the recorded output device has been quiet for a while, it listens
    to the other outputs and follows whichever is playing (only one plays the call); and the
    microphone is whichever connected headset is live (`headset_microphone_name` first, then any
    recognized by name), the usual microphone only when none is — see next_system_device /
    next_microphone.
    Listening is done on separate streams, so recording carries on throughout, and every switch lands
    in capture_notices() with its reason. Picking a device by hand mid-meeting turns that stream's
    automation off for the rest of the meeting — the person has decided.
    """

    # How often the background thread checks whether the set of audio devices, or the Windows default,
    # has changed (see reload_devices). Frequent enough that a headset plugged in mid-meeting shows up
    # within a few seconds; each check is a handful of cheap winmm queries.
    _DEVICE_POLL_SECONDS = 3.0

    def __init__(
        self,
        output_dir: Path,
        mic_device_name: str | None = None,
        system_device_name: str | None = None,
        *,
        auto_system_device: bool = False,
        auto_microphone: bool = False,
        headset_microphone_name: str | None = None,
    ):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._mic_device_name = mic_device_name
        self._system_device_name = system_device_name
        # The microphone chosen in Settings, which _mic_device_name stops being after a switch — kept as
        # the one to fall back to when the headset goes quiet.
        self._configured_mic_name = mic_device_name
        self._auto_system = auto_system_device
        self._auto_mic = auto_microphone
        self._headset_microphone_name = headset_microphone_name
        self._auto_thread: threading.Thread | None = None
        self._next_system_search = 0.0
        self._next_headset_check = 0.0
        # Held while a probe has streams open on self._pyaudio, so reload_devices() can't terminate it
        # under them. Never held while taking _lifecycle_lock, so the two can't deadlock.
        self._probe_lock = threading.Lock()
        # Set by stop(): nothing may open a new capture stream after that.
        self._stopping = False
        # The device actually in use right now, as opposed to _mic_device_name/_system_device_name above
        # (the requested name, which stays None for "follow the Windows default") — what the default-
        # device watcher compares Windows' current default against to notice it changed.
        self._mic_active_device_name: str | None = None
        self._system_active_device_name: str | None = None
        self._pyaudio = None
        self._pyaudio_module = None
        self._mic_thread: threading.Thread | None = None
        self._system_thread: threading.Thread | None = None
        self._mic_writer: _SegmentedWavWriter | None = None
        self._system_writer: _SegmentedWavWriter | None = None
        self._mic_monitor = _StreamActivityMonitor()
        self._system_monitor = _StreamActivityMonitor()
        self._mic_stop_event = threading.Event()
        self._system_stop_event = threading.Event()
        self._watch_stop_event = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        # Capture threads a device switch gave up waiting for (see _switch_stream) — still blocked in a
        # native stream.read() on the device switched away from. stop() must not terminate PortAudio
        # while any of these is alive, same as for the current threads.
        self._orphaned_threads: list[tuple[threading.Thread, str]] = []
        # Serializes stop() against switch_mic_device()/switch_system_device(), and switches against
        # each other. Without this, a switch racing stop() could still be opening a new stream on
        # self._pyaudio in one thread while stop() is terminating that same PyAudio instance in
        # another — exactly the kind of WASAPI/COM teardown race _pyaudio_lifecycle_lock exists to
        # prevent across *different* Recorder instances, but that lock doesn't cover this in-instance
        # case.
        self._lifecycle_lock = threading.Lock()
        self._started_at: float | None = None
        self._mic_path = self._output_dir / "mic.wav"
        self._system_path = self._output_dir / "system.wav"
        self._notices: list[str] = []
        self._notices_lock = threading.Lock()
        self.mic_level = 0.0
        self.system_level = 0.0
        # Bumped each time reload_devices() gives PortAudio a fresh device list — see available_devices.
        self.device_list_version = 0
        # Injectable for tests; see audio.device_watch.
        self._device_signature: Callable[[], object | None] = device_signature
        # Injectable for tests; see audio.call_devices. Asked by the automation (auto_* above) before
        # any of its own guessing: when Teams has a microphone or speaker open, that's the one.
        self._call_devices: Callable[[], CallAudioDevices | None] = find_call_audio_devices
        self._call_devices_cache: tuple[float, CallAudioDevices | None] | None = None
        # Set by a capture thread that died on its own (not stopped or replaced), so the device watcher
        # reopens it — see _recover_failed_streams.
        self._stream_failed = threading.Event()
        self._recovery_attempts = 0
        self._next_recovery_at = 0.0
        self._last_recovery_at: float | None = None

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "Audio recording requires Windows (WASAPI loopback via PyAudioWPatch); "
                f"unsupported platform: {sys.platform!r}"
            )
        import pyaudiowpatch as pyaudio

        self._pyaudio_module = pyaudio
        with _pyaudio_lifecycle_lock:
            self._pyaudio = pyaudio.PyAudio()
        self._mic_stop_event.clear()
        self._system_stop_event.clear()
        self._started_at = time.monotonic()

        call = self._call_app_devices(time.monotonic()) if (self._auto_mic or self._auto_system) else None
        call_mic = self._call_app_input(call) if self._auto_mic else None
        call_speaker = self._call_app_loopback(call) if self._auto_system else None
        if call_mic is not None:
            if call_mic != self._mic_device_name:
                self._mic_device_name = call_mic
                self._record_switch(f"Microphone set to {call_mic!r} — Teams is using it.")
        elif self._auto_mic:
            # The preferred headset to begin with; the automation's first check, straight after start,
            # listens to every connected headset and moves to one that's live if this one isn't.
            headsets = self._connected_headsets()
            if headsets and headsets[0] != self._mic_device_name:
                self._mic_device_name = headsets[0]
                self._record_switch(f"Microphone set to {headsets[0]!r} — a headset is connected.")
        if call_speaker is not None and call_speaker != self._system_device_name:
            self._system_device_name = call_speaker
            self._record_switch(f"System audio set to {call_speaker!r} — Teams is playing the call through it.")
        mic_info = self._resolve_input_device(self._mic_device_name)
        system_info = self._resolve_loopback_device(pyaudio, self._system_device_name)
        self._mic_active_device_name = mic_info["name"]
        self._system_active_device_name = system_info["name"]

        self._mic_writer = _SegmentedWavWriter(
            self._mic_path, int(mic_info["maxInputChannels"]), SAMPLE_WIDTH_BYTES,
            int(mic_info["defaultSampleRate"]),
        )
        self._mic_thread = self._spawn_capture_thread(
            pyaudio, mic_info, self._mic_writer, "mic_level", self._mic_monitor, "Microphone",
            self._mic_stop_event,
        )

        self._system_writer = _SegmentedWavWriter(
            self._system_path, int(system_info["maxInputChannels"]), SAMPLE_WIDTH_BYTES,
            int(system_info["defaultSampleRate"]),
        )
        self._system_thread = self._spawn_capture_thread(
            pyaudio, system_info, self._system_writer, "system_level", self._system_monitor,
            "System audio", self._system_stop_event,
        )

        self._watch_stop_event.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch_devices, daemon=True, name="recorder-device-watch"
        )
        self._watcher_thread.start()
        if self._auto_system or self._auto_mic:
            self._auto_thread = threading.Thread(
                target=self._auto_select_devices, daemon=True, name="recorder-auto-device"
            )
            self._auto_thread.start()

    def switch_mic_device(
        self, device_name: str | None, *, reason: str | None = None, automatic: bool = False
    ) -> None:
        """Moves the microphone stream to a different device without stopping the meeting. The system-
        audio stream is untouched. `device_name` follows the same convention as the constructor: None
        means "whatever Windows currently considers the default". `reason`, if given, replaces the
        generic wording in the notice this records. A switch that isn't `automatic` is someone's own
        choice, and turns automatic microphone selection off for the rest of the meeting."""
        if not automatic and self._auto_mic:
            self._auto_mic = False
            self._record_notice(
                "Automatic microphone selection is off for the rest of this meeting, since a microphone "
                "was picked by hand."
            )
        with self._lifecycle_lock:
            self._switch_stream(
                stop_event=self._mic_stop_event,
                writer=self._mic_writer,
                level_attr="mic_level",
                monitor=self._mic_monitor,
                label="Microphone",
                thread_attr="_mic_thread",
                device_name_attr="_mic_device_name",
                active_name_attr="_mic_active_device_name",
                resolve=lambda: self._resolve_input_device(device_name),
                device_name=device_name,
                reason=reason,
            )

    def switch_system_device(
        self, device_name: str | None, *, reason: str | None = None, automatic: bool = False
    ) -> None:
        """Moves the system-audio (loopback) stream to a different device without stopping the meeting.
        The microphone stream is untouched. See switch_mic_device for `reason` and `automatic`."""
        if not automatic and self._auto_system:
            self._auto_system = False
            self._record_notice(
                "Automatic system-audio selection is off for the rest of this meeting, since a device "
                "was picked by hand."
            )
        with self._lifecycle_lock:
            self._switch_stream(
                stop_event=self._system_stop_event,
                writer=self._system_writer,
                level_attr="system_level",
                monitor=self._system_monitor,
                label="System audio",
                thread_attr="_system_thread",
                device_name_attr="_system_device_name",
                active_name_attr="_system_active_device_name",
                resolve=lambda: self._resolve_loopback_device(self._pyaudio_module, device_name),
                device_name=device_name,
                reason=reason,
            )

    def _switch_stream(
        self,
        *,
        stop_event: threading.Event,
        writer: _SegmentedWavWriter | None,
        level_attr: str,
        monitor: _StreamActivityMonitor,
        label: str,
        thread_attr: str,
        device_name_attr: str,
        active_name_attr: str,
        resolve: Callable[[], dict],
        device_name: str | None,
        reason: str | None,
    ) -> None:
        if self._started_at is None:
            raise RuntimeError("Recorder.start() was never called")
        if writer is None:
            raise RuntimeError(f"{label} stream was never started")
        if self._stopping:
            raise RuntimeError("the recording has already stopped")

        # Stop only this stream's capture thread — the other one is left running, which is the whole
        # point of switching one device without restarting the meeting.
        old_thread: threading.Thread | None = getattr(self, thread_attr)
        # Retired *before* the stop event is set, and for good: stop_event is shared with the thread
        # spawned below and gets cleared for it, so on its own it can't keep a stuck old thread stopped —
        # one that unblocked later used to see the cleared event and carry on reading the old device into
        # the new part alongside its replacement, then close the writer out from under it when it finally
        # did stop. A retired thread never writes again and leaves the writer to start_new_part below.
        retired = getattr(old_thread, "retired", None)
        if retired is not None:
            retired.set()
        stop_event.set()
        if old_thread is not None and self._join_capture_thread(old_thread):
            self._orphaned_threads.append((old_thread, label))
            # Still running after the timeout — presumably blocked inside a native, uninterruptible
            # stream.read() on the device being switched away from (a hung or disconnected driver). The
            # switch still has to proceed (there's no way to interrupt a blocked native read from here),
            # but the writer object it's still holding a reference to is the same one the freshly spawned
            # thread below is about to write to — worth surfacing rather than silently racing.
            self._record_notice(
                f"{label} capture thread from before the switch didn't stop within 5s — it may still be "
                "reading from the previous device in the background."
            )

        device_info = resolve()
        setattr(self, device_name_attr, device_name)
        setattr(self, active_name_attr, device_info["name"])

        # A new device can have a different channel count or sample rate than the old one, which a WAV
        # file can't change mid-stream — roll over into a new part with the new device's parameters
        # instead. Transcription already stitches parts back onto one clock (see
        # transcription.engine.transcribe_parts), which is exactly what's needed here too.
        writer.start_new_part(int(device_info["maxInputChannels"]), int(device_info["defaultSampleRate"]))

        stop_event.clear()
        new_thread = self._spawn_capture_thread(
            self._pyaudio_module, device_info, writer, level_attr, monitor, label, stop_event
        )
        setattr(self, thread_attr, new_thread)
        if reason is None:
            self._record_switch(f"{label} switched to {device_info['name']!r} mid-meeting.")
        else:
            self._record_switch(f"{label} switched to {device_info['name']!r} — {reason}.")

    def _watch_devices(self) -> None:
        """Runs on its own thread for the life of the meeting, reloading the device list whenever the
        set of devices or the Windows default changes — see the class docstring.

        `Event.wait(timeout)` both sleeps between checks and returns True the instant stop() sets
        _watch_stop_event, so this exits promptly on stop rather than finishing out a long poll interval.
        """
        last = self._safe_device_signature()
        while not self._watch_stop_event.wait(self._poll_interval()):
            current = self._safe_device_signature()
            if current is not None and last is not None and current != last:
                try:
                    self.reload_devices(reason="audio devices changed")
                except Exception as exc:
                    self._record_capture_failure("Audio devices", exc)
                # Reopening both streams is also what recovery would have done.
                self._stream_failed.clear()
            elif self._stream_failed.is_set():
                self._recover_failed_streams(time.monotonic())
            if current is not None:
                last = current  # can't tell right now: compare against the last good reading next time

    def _poll_interval(self) -> float:
        # Recovery is checked on the same loop; a stream that died shouldn't wait out the full interval.
        if self._stream_failed.is_set():
            return min(self._DEVICE_POLL_SECONDS, _RECOVERY_FIRST_DELAY_SECONDS)
        return self._DEVICE_POLL_SECONDS

    def _recover_failed_streams(self, now: float) -> None:
        """Reopens a capture stream that died mid-meeting — see _RECOVERY_*. Goes through
        reload_devices, since the usual reason a stream dies is that the device it was reading changed
        underneath it (a Bluetooth headset's profile switch replaces its endpoints outright), and only a
        fresh PortAudio device list can see what replaced it."""
        if now < self._next_recovery_at:
            return
        if self._last_recovery_at is not None and now - self._last_recovery_at >= _RECOVERY_RESET_AFTER_SECONDS:
            self._recovery_attempts = 0
        self._stream_failed.clear()
        self._recovery_attempts += 1
        self._last_recovery_at = now
        self._next_recovery_at = now + min(
            _RECOVERY_MAX_DELAY_SECONDS, _RECOVERY_FIRST_DELAY_SECONDS * 2 ** (self._recovery_attempts - 1)
        )
        try:
            self.reload_devices(reason="reopened after the stream stopped")
        except Exception as exc:
            self._record_capture_failure("Audio devices", exc)
            self._stream_failed.set()
            return
        if not self._stream_failed.is_set():
            self._record_switch("Audio capture reopened after a stream stopped mid-meeting — recording continues.")

    def _auto_select_devices(self) -> None:
        """Runs on its own thread for the life of the meeting when either automation is on — see the
        class docstring. Exits promptly on stop(), same as _watch_devices; a probe in progress is cut
        short by the same event. The microphone is checked straight away, rather than after the first
        interval, so a meeting that starts on a headset that's switched off moves off it in a second
        rather than after _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS."""
        steps = [lambda now: self._auto_select_microphone(now, starting=True)]
        while True:
            for step in steps:
                try:
                    step(time.monotonic())
                except Exception as exc:  # a background check must never take the meeting down
                    self._record_notice(
                        f"Automatic device selection skipped a check ({type(exc).__name__}: {exc})."
                    )
            if self._watch_stop_event.wait(_AUTO_DEVICE_POLL_SECONDS):
                return
            steps = [self._auto_select_system_device, self._auto_select_microphone]

    def _call_app_devices(self, now: float) -> CallAudioDevices | None:
        """What Teams is using right now (see audio.call_devices), briefly cached."""
        cached = self._call_devices_cache
        if cached is not None and now - cached[0] < _CALL_DEVICES_CACHE_SECONDS:
            return cached[1]
        try:
            devices = self._call_devices()
        except Exception:
            devices = None
        self._call_devices_cache = (now, devices)
        return devices

    def _call_app_input(self, call: CallAudioDevices | None) -> str | None:
        """The input device, by this recorder's PortAudio name, that Teams has open — the one being
        recorded if Teams has that one open among others. None if Teams isn't using one, or only ones
        PortAudio doesn't list (yet: a reload follows a device change)."""
        if call is None or not call.microphones or self._pyaudio is None:
            return None
        from meeting_scribe.audio.device_picker import input_devices_from

        try:
            names = [device.name for device in input_devices_from(self._pyaudio)]
        except Exception:
            return None
        return self._pick_call_device(call.microphones, names, self._mic_active_device_name)

    def _call_app_loopback(self, call: CallAudioDevices | None) -> str | None:
        """The loopback device for the output Teams is playing the call through — see _call_app_input."""
        if call is None or not call.speakers or self._pyaudio is None:
            return None
        try:
            names = [info["name"] for info in self._pyaudio.get_loopback_device_info_generator()]
        except Exception:
            return None
        names = [name for name in names if name.endswith("[Loopback]")]
        return self._pick_call_device(call.speakers, names, self._system_active_device_name)

    @staticmethod
    def _pick_call_device(endpoints, portaudio_names, active: str | None) -> str | None:
        matched = [
            name for name in (match_portaudio_device(endpoint, portaudio_names) for endpoint in endpoints) if name
        ]
        if active is not None:
            for name in matched:
                if _same_device(active, name):
                    return active  # already on one of them: stay, rather than hop between them
        return matched[0] if matched else None

    def _auto_select_system_device(self, now: float) -> None:
        if not self._auto_system:
            return
        with self._probe_lock:
            target = self._call_app_loopback(self._call_app_devices(now))
        if target is not None:
            # Teams says where the call is coming out — no need to wait for the recorded device to go
            # quiet and listen around for it.
            active = self._system_active_device_name
            if (active is None or not _same_device(active, target)) and not self._watch_stop_event.is_set():
                self.switch_system_device(target, reason="Teams is playing the call through it", automatic=True)
            return
        if now < self._next_system_search:
            return
        quiet = self.system_health().seconds_without_sound
        if quiet is None or quiet < _SYSTEM_QUIET_BEFORE_SEARCH_SECONDS:
            return
        self._next_system_search = now + _SYSTEM_SEARCH_INTERVAL_SECONDS
        active = self._system_active_device_name
        with self._probe_lock:
            pyaudio_instance = self._pyaudio
            if pyaudio_instance is None:
                return
            candidates = [
                info for info in pyaudio_instance.get_loopback_device_info_generator()
                if info.get("name") != active
            ]
            probes = _probe_devices(
                pyaudio_instance, self._pyaudio_module, candidates, _PROBE_SECONDS, self._watch_stop_event
            )
        choice = next_system_device(active=active, active_seconds_without_sound=quiet, probes=probes)
        if choice is not None and self._auto_system and not self._watch_stop_event.is_set():
            self.switch_system_device(choice, reason="the call's sound is playing through it", automatic=True)

    def _auto_select_microphone(self, now: float, *, starting: bool = False) -> None:
        """One check of the microphone — see next_microphone. Headsets are listened to only when that
        could change anything: at the start of the meeting (all of them, the one being recorded
        included), once the one being recorded has gone silent (the others), and every
        _HEADSET_RECHECK_SECONDS while on the fallback (all of them)."""
        if not self._auto_mic:
            return
        active = self._mic_active_device_name
        with self._probe_lock:
            target = self._call_app_input(self._call_app_devices(now))
        if target is not None:
            # Teams says which microphone it has open, which beats any guess made from names or levels —
            # including a headset that isn't called one, or a laptop mic Teams was deliberately set to.
            if (active is None or not _same_device(active, target)) and not self._watch_stop_event.is_set():
                self.switch_mic_device(target, reason="Teams is using this microphone", automatic=True)
            return
        silent_for = self.mic_health().seconds_without_signal
        active_is_dead = silent_for is not None and silent_for >= _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS
        with self._probe_lock:
            pyaudio_instance = self._pyaudio
            if pyaudio_instance is None:
                return
            from meeting_scribe.audio.device_picker import input_devices_from

            names = [device.name for device in input_devices_from(pyaudio_instance)]
            headsets = find_headset_microphones(names, self._headset_microphone_name)
            if not headsets:
                return
            try:
                default_name = pyaudio_instance.get_default_input_device_info().get("name")
            except Exception:  # no default input at all
                default_name = None
            fallback = choose_fallback_microphone(
                names, configured=self._configured_mic_name, default_name=default_name, headsets=headsets
            )
            on_headset = active is not None and any(_same_device(active, h) for h in headsets)
            if starting:
                to_probe = list(headsets)
            elif on_headset:
                to_probe = [h for h in headsets if not _same_device(h, active)] if active_is_dead else []
            elif now >= self._next_headset_check:
                to_probe = list(headsets)
            else:
                to_probe = []
            live_headsets = None
            if to_probe:
                if not on_headset:
                    self._next_headset_check = now + _HEADSET_RECHECK_SECONDS
                infos = [info for info in (_find_input_device(pyaudio_instance, h) for h in to_probe) if info]
                probes = _probe_devices(
                    pyaudio_instance, self._pyaudio_module, infos, _PROBE_SECONDS, self._watch_stop_event
                )
                live_headsets = [probe.name for probe in probes if probe.heard_signal]
                if starting and on_headset and not any(_same_device(active, h) for h in live_headsets):
                    active_is_dead = True
        choice = next_microphone(
            active=active,
            headsets=headsets,
            fallback=fallback,
            active_is_dead=active_is_dead,
            live_headsets=live_headsets,
        )
        if choice is None or not self._auto_mic or self._watch_stop_event.is_set():
            return
        if any(_same_device(choice, h) for h in headsets):
            reason = "that headset is connected and picking up sound"
            if on_headset:
                reason = "the headset in use isn't picking anything up, and this one is"
        else:
            reason = "no headset is picking anything up (muted, or switched off?)"
            self._next_headset_check = now + _HEADSET_RECHECK_SECONDS
        self.switch_mic_device(choice, reason=reason, automatic=True)

    def _connected_headsets(self) -> list[str]:
        from meeting_scribe.audio.device_picker import input_devices_from

        try:
            names = [device.name for device in input_devices_from(self._pyaudio)]
        except Exception:
            return []
        return find_headset_microphones(names, self._headset_microphone_name)

    def _safe_device_signature(self):
        # Swallows everything: this is a background poll, not something a transient enumeration hiccup
        # should ever be allowed to take the meeting down over. It just tries again next tick.
        try:
            return self._device_signature()
        except Exception:
            return None

    def reload_devices(self, *, reason: str = "the device list was refreshed") -> None:
        """Re-initializes PortAudio so devices added, removed, or made default since the meeting started
        become visible, and reopens both streams on it — see the class docstring. Called by the device
        watcher, and by the GUI's "Refresh devices" button while a meeting is recording (the only way
        that button can show a current list then — see available_devices).

        Never terminates PortAudio under a capture thread that won't stop (same rule as stop()): if
        either thread is stuck, the streams are reopened on the existing, stale list instead and a
        notice says why."""
        with self._lifecycle_lock:
            if self._started_at is None:
                raise RuntimeError("Recorder.start() was never called")
            streams = (
                ("Microphone", "_mic_thread", "_mic_stop_event", "_mic_writer", "mic_level",
                 self._mic_monitor, "_mic_device_name", "_mic_active_device_name",
                 lambda name: self._resolve_input_device(name)),
                ("System audio", "_system_thread", "_system_stop_event", "_system_writer", "system_level",
                 self._system_monitor, "_system_device_name", "_system_active_device_name",
                 lambda name: self._resolve_loopback_device(self._pyaudio_module, name)),
            )

            old_threads = [getattr(self, stream[1]) for stream in streams]
            for thread, stream in zip(old_threads, streams):
                retired = getattr(thread, "retired", None)
                if retired is not None:
                    retired.set()
                getattr(self, stream[2]).set()
            stuck = False
            for thread, stream in zip(old_threads, streams):
                if thread is not None and self._join_capture_thread(thread):
                    self._orphaned_threads.append((thread, stream[0]))
                    stuck = True

            if stuck:
                self._record_notice(
                    "Couldn't reload the audio device list — a capture thread is still stuck on its "
                    "previous device, and restarting audio under it could crash the app. Newly connected "
                    "devices won't be available until the next meeting."
                )
            else:
                try:
                    with self._probe_lock, _pyaudio_lifecycle_lock:
                        if self._pyaudio is not None:
                            self._pyaudio.terminate()
                        self._pyaudio = None
                        self._pyaudio = self._pyaudio_module.PyAudio()
                except Exception as exc:
                    # Nothing can be opened without PortAudio; say so. The next device change (or
                    # Refresh devices) tries again from here, since _pyaudio is left as None.
                    self._record_switch(
                        f"Couldn't restart audio to reload the device list ({type(exc).__name__}: {exc}) "
                        "— recording is paused until audio can be restarted; retrying."
                    )
                    self._stream_failed.set()
                    return

            for stream in streams:
                (label, thread_attr, event_attr, writer_attr, level_attr, monitor, name_attr, active_attr,
                 resolve) = stream
                writer: _SegmentedWavWriter | None = getattr(self, writer_attr)
                if writer is None:
                    continue
                setattr(self, thread_attr, None)
                try:
                    device_info = resolve(getattr(self, name_attr))
                except Exception as exc:  # e.g. every output device unplugged: nothing to loop back from
                    self._record_capture_failure(label, exc)
                    self._stream_failed.set()  # try again shortly; the device may be back by then
                    continue
                previous = getattr(self, active_attr)
                setattr(self, active_attr, device_info["name"])
                writer.start_new_part(
                    int(device_info["maxInputChannels"]), int(device_info["defaultSampleRate"])
                )
                stop_event: threading.Event = getattr(self, event_attr)
                stop_event.clear()
                setattr(self, thread_attr, self._spawn_capture_thread(
                    self._pyaudio_module, device_info, writer, level_attr, monitor, label, stop_event
                ))
                if device_info["name"] != previous:
                    self._record_switch(f"{label} switched to {device_info['name']!r} — {reason}.")
            self.device_list_version += 1

    def available_devices(self) -> tuple[list, list]:
        """(input devices, loopback devices) as this Recorder's PortAudio currently sees them — the
        current list, including anything plugged in mid-meeting once reload_devices() has run. While a
        meeting records, a fresh device_picker enumeration would only return the stale snapshot from
        when the meeting started (see audio.device_watch)."""
        from meeting_scribe.audio.device_picker import input_devices_from, loopback_devices_from

        with self._lifecycle_lock:
            if self._pyaudio is None:
                return [], []
            return input_devices_from(self._pyaudio), loopback_devices_from(self._pyaudio)

    def stop(self) -> RecordedAudio:
        if self._started_at is None:
            raise RuntimeError("Recorder.start() was never called")
        # Stopped and joined before the lifecycle lock below, rather than under it: if a switch it
        # triggered is mid-flight holding that lock, this waits for it to finish naturally (the watcher
        # loop then sees _watch_stop_event and exits) instead of tearing down self._pyaudio while that
        # switch might still be opening a stream on it.
        self._watch_stop_event.set()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=5)
        if self._auto_thread is not None:
            self._auto_thread.join(timeout=10)
        with self._lifecycle_lock:
            self._stopping = True
            self._mic_stop_event.set()
            self._system_stop_event.set()
            stuck_labels = []
            for thread, label, writer in (
                (self._mic_thread, "Microphone", self._mic_writer),
                (self._system_thread, "System audio", self._system_writer),
            ):
                if thread is not None and self._join_capture_thread(thread):
                    stuck_labels.append(label)
                    # The thread closes its writer on the way out, and this one can't get out — so close
                    # it here, or the file is left open, and without a header at all if the device never
                    # delivered a chunk (the header is only written with the first one), which nothing
                    # can then read. Safe to do under the stuck thread: the writer is locked, and if the
                    # thread ever wakes, its next write raises "already closed" and it exits.
                    if writer is not None:
                        try:
                            writer.close()
                        except Exception as exc:
                            self._record_capture_failure(label, exc)
            stuck_labels += [label for thread, label in self._orphaned_threads if thread.is_alive()]
            if self._pyaudio is not None:
                if stuck_labels:
                    # A capture thread still running here is presumably blocked inside a native,
                    # uninterruptible PortAudio stream.read() call (a hung or disconnected device).
                    # Terminating PortAudio out from under a thread that might still be reading from it is
                    # a use-after-free from that thread's point of view — exactly the kind of thing that
                    # crashes the whole process natively rather than raising anything Python could catch,
                    # here or anywhere else. Leaking this PyAudio instance instead is the safer trade:
                    # Windows reclaims its handles when the process exits regardless.
                    for label in stuck_labels:
                        self._record_notice(
                            f"{label} capture thread didn't stop within 5s — its device may be "
                            "unresponsive. Its resources were left in place rather than risk crashing "
                            "the app by tearing down audio while it might still be in use."
                        )
                else:
                    with _pyaudio_lifecycle_lock:
                        self._pyaudio.terminate()
            # A stream whose device couldn't be reopened after a reload has no thread left to close its
            # writer, which would leave its last part open. close() is idempotent, so closing every
            # writer here is safe for the ones a thread already closed.
            for label, thread, writer in (
                ("Microphone", self._mic_thread, self._mic_writer),
                ("System audio", self._system_thread, self._system_writer),
            ):
                if writer is not None and label not in stuck_labels and (thread is None or not thread.is_alive()):
                    try:
                        writer.close()
                    except Exception as exc:
                        self._record_capture_failure(label, exc)
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

    def _record_switch(self, message: str) -> None:
        # Not deduplicated: switching back and forth between the same two devices should show every
        # switch, not just the first.
        with self._notices_lock:
            self._notices.append(message)

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

    @staticmethod
    def _join_capture_thread(thread: threading.Thread, timeout: float = 5.0) -> bool:
        """Waits up to `timeout` seconds for a capture thread to notice its stop event and exit, and
        returns whether it's still running afterward. A thread still alive here is presumably stuck
        inside a native, uninterruptible PortAudio call (stream.read() on a hung or disconnected device)
        rather than merely slow — the caller must not then tear down anything (the shared PyAudio
        instance, the shared _SegmentedWavWriter) that thread might still be touching. See stop() and
        _switch_stream()."""
        thread.join(timeout=timeout)
        return thread.is_alive()

    def _spawn_capture_thread(
        self,
        pyaudio_module,
        device_info: dict,
        writer: _SegmentedWavWriter,
        level_attr: str,
        monitor: _StreamActivityMonitor,
        label: str,
        stop_event: threading.Event,
    ) -> threading.Thread:
        """Starts one capture thread reading `device_info` into `writer` until `stop_event` is set —
        either the meeting stopping (see stop()) or this one stream being switched to a different device
        (see switch_mic_device/switch_system_device), which set only that stream's event and hand the
        same writer to a freshly spawned thread on the new device, so the writer's WAV parts stay
        continuous across the switch. `writer.close()` always runs when this thread ends, which finalizes
        whatever part is currently open — the last one on a real stop, or the one being rolled over on a
        switch."""
        channels = int(device_info["maxInputChannels"])
        rate = int(device_info["defaultSampleRate"])
        # Set by _switch_stream when this thread is being replaced — see there.
        retired = threading.Event()
        # A loopback stream is read only as far as it has data (see _read_what_is_available): a blocking
        # read there waits for as long as the output device is silent, which left the thread unable to
        # stop — so switching away from it, or ending the meeting, had to give up on it.
        poll = bool(device_info.get("isLoopbackDevice"))

        bytes_per_frame = channels * SAMPLE_WIDTH_BYTES

        def run() -> None:
            nonlocal poll
            polled_once = False
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
                opened_at = last_data_at = time.monotonic()
                frames_written = 0
                monitor.stream_opened(opened_at)
                while not (stop_event.is_set() or retired.is_set()):
                    if poll:
                        try:
                            data = _read_what_is_available(stream)
                        except Exception:
                            if polled_once:
                                raise
                            poll = False  # this stream can't say what it has buffered; read normally
                            continue
                        polled_once = True
                        if not data:
                            # See _LOOPBACK_GAP_SECONDS: nothing is playing, so write the silence
                            # WASAPI doesn't, up to now.
                            now = time.monotonic()
                            if now - last_data_at >= _LOOPBACK_GAP_SECONDS:
                                missing = int((now - opened_at) * rate) - frames_written
                                if missing > rate * _MAX_LOOPBACK_FILL_SECONDS:
                                    # Far more than one pass of this loop can fall behind: the machine
                                    # was asleep. The microphone recorded nothing then either, so
                                    # carry on from here rather than write that out as silence.
                                    frames_written += missing
                                    missing = 0
                                if missing > 0 and not retired.is_set():
                                    writer.write(bytes(missing * bytes_per_frame))
                                    frames_written += missing
                                    setattr(self, level_attr, 0.0)
                            stop_event.wait(0.01)
                            continue
                    else:
                        data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                    if retired.is_set():
                        break  # replaced while blocked in read(); the writer belongs to the new thread now
                    writer.write(data)
                    frames_written += len(data) // bytes_per_frame
                    last_data_at = time.monotonic()
                    stats = _analyze_pcm16(data)
                    setattr(self, level_attr, stats.level)
                    monitor.observe(stats, time.monotonic())
            except Exception as exc:
                # Nothing is watching this thread, so an escaping exception would be invisible; record
                # it for the GUI and for RecordedAudio.notices, and still close everything below.
                self._record_capture_failure(label, exc)
                if not (stop_event.is_set() or retired.is_set() or self._stopping):
                    self._stream_failed.set()  # the device watcher reopens it — see _recover_failed_streams
            finally:
                for close in (
                    getattr(stream, "stop_stream", None),
                    getattr(stream, "close", None),
                    None if retired.is_set() else writer.close,
                ):
                    try:
                        if close is not None:
                            close()
                    except Exception as exc:  # one failed teardown step shouldn't skip the rest
                        self._record_capture_failure(label, exc)
                if not retired.is_set():
                    setattr(self, level_attr, 0.0)

        thread = threading.Thread(target=run, daemon=True, name=f"recorder-{label.lower().replace(' ', '-')}")
        thread.retired = retired
        thread.start()
        return thread
