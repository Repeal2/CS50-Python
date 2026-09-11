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


def _pcm16_level(data: bytes) -> float:
    """RMS level of a 16-bit PCM chunk on a logarithmic (dBFS) scale, normalized to 0..1 for a VU-meter
    display. Not calibrated to any broadcast standard — it only needs to make normal speaking volume
    visibly move the meter and sit near zero with silence, so a user can tell at a glance whether a
    device is actually picking anything up."""
    if not data:
        return 0.0
    samples = np.frombuffer(data, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
    if rms <= 0:
        return 0.0
    db = 20.0 * np.log10(rms / 32768.0)
    return float(np.clip((db - _SILENCE_FLOOR_DB) / -_SILENCE_FLOOR_DB, 0.0, 1.0))


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

    `errors` holds anything that killed a capture thread mid-meeting, as human-readable text, so a
    half-recorded meeting says so instead of just coming back quiet."""

    mic_paths: tuple[Path, ...]
    system_paths: tuple[Path, ...]
    started_at_monotonic: float
    errors: tuple[str, ...] = ()


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
    meeting with it — the other stream keeps recording — but it does record why, in `capture_errors()`,
    which the GUI polls alongside the levels so a dead microphone is visible while the meeting is still
    running rather than a surprise in an empty transcript afterwards.
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
        self._stop_event = threading.Event()
        self._started_at: float | None = None
        self._mic_path = self._output_dir / "mic.wav"
        self._system_path = self._output_dir / "system.wav"
        self._capture_errors: list[str] = []
        self._capture_errors_lock = threading.Lock()
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
            pyaudio, mic_info, self._mic_path, "mic_level", "Microphone"
        )
        self._system_writer, self._system_thread = self._spawn_capture_thread(
            pyaudio, system_info, self._system_path, "system_level", "System audio"
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
        return RecordedAudio(
            mic_paths=self._written_paths(self._mic_writer, self._mic_path),
            system_paths=self._written_paths(self._system_writer, self._system_path),
            started_at_monotonic=self._started_at,
            errors=self.capture_errors(),
        )

    def capture_errors(self) -> tuple[str, ...]:
        """Whatever has killed a capture thread so far, newest last — empty while both streams are
        healthy. Safe to poll from another thread at any time, including mid-recording."""
        with self._capture_errors_lock:
            return tuple(self._capture_errors)

    @staticmethod
    def _written_paths(writer: _SegmentedWavWriter | None, fallback: Path) -> tuple[Path, ...]:
        # The fallback covers a stream whose thread never got as far as opening a file at all; the empty
        # WAV it names may not exist, which transcription already tolerates.
        return writer.paths if writer is not None else (fallback,)

    def _record_capture_error(self, label: str, exc: BaseException) -> None:
        with self._capture_errors_lock:
            self._capture_errors.append(
                f"{label} capture stopped early: {type(exc).__name__}: {exc}"
            )

    def _resolve_input_device(self, name: str | None) -> dict:
        if name:
            for i in range(self._pyaudio.get_device_count()):
                info = self._pyaudio.get_device_info_by_index(i)
                if info.get("name") == name and info.get("maxInputChannels", 0) > 0:
                    return info
        return self._pyaudio.get_default_input_device_info()

    def _resolve_loopback_device(self, pyaudio_module, name: str | None) -> dict:
        if name:
            for loopback in self._pyaudio.get_loopback_device_info_generator():
                if loopback.get("name") == name:
                    return loopback
        return self._get_default_loopback_device(pyaudio_module)

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
        self, pyaudio_module, device_info: dict, path: Path, level_attr: str, label: str
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
                    setattr(self, level_attr, _pcm16_level(data))
            except Exception as exc:
                # Nothing is watching this thread, so an escaping exception would be invisible; record
                # it for the GUI and for RecordedAudio.errors, and still close everything below.
                self._record_capture_error(label, exc)
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
                        self._record_capture_error(label, exc)
                setattr(self, level_attr, 0.0)

        thread = threading.Thread(target=run, daemon=True, name=f"recorder-{path.stem}")
        thread.start()
        return writer, thread
