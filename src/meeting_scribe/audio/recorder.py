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


def _pcm16_level(data: bytes) -> float:
    """RMS level of a 16-bit PCM chunk, normalized to roughly 0..1 for a VU-meter display. Not
    calibrated to any standard (e.g. dBFS) — it only needs to visibly move with real audio and sit near
    zero with silence, so a user can tell at a glance whether a device is actually picking anything up."""
    if not data:
        return 0.0
    samples = np.frombuffer(data, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
    return min(1.0, rms / 32768.0)


@dataclass(frozen=True)
class RecordedAudio:
    mic_path: Path
    system_path: Path
    started_at_monotonic: float


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
        self._stop_event = threading.Event()
        self._started_at: float | None = None
        self._mic_path = self._output_dir / "mic.wav"
        self._system_path = self._output_dir / "system.wav"
        self.mic_level = 0.0
        self.system_level = 0.0

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "Audio recording requires Windows (WASAPI loopback via PyAudioWPatch); "
                f"unsupported platform: {sys.platform!r}"
            )
        import pyaudiowpatch as pyaudio

        self._pyaudio = pyaudio.PyAudio()
        self._stop_event.clear()
        self._started_at = time.monotonic()

        mic_info = self._resolve_input_device(self._mic_device_name)
        system_info = self._resolve_loopback_device(pyaudio, self._system_device_name)

        self._mic_thread = self._spawn_capture_thread(pyaudio, mic_info, self._mic_path, "mic_level")
        self._system_thread = self._spawn_capture_thread(
            pyaudio, system_info, self._system_path, "system_level"
        )

    def stop(self) -> RecordedAudio:
        if self._started_at is None:
            raise RuntimeError("Recorder.start() was never called")
        self._stop_event.set()
        for thread in (self._mic_thread, self._system_thread):
            if thread is not None:
                thread.join(timeout=5)
        if self._pyaudio is not None:
            self._pyaudio.terminate()
        return RecordedAudio(
            mic_path=self._mic_path,
            system_path=self._system_path,
            started_at_monotonic=self._started_at,
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
        self, pyaudio_module, device_info: dict, path: Path, level_attr: str
    ) -> threading.Thread:
        channels = int(device_info["maxInputChannels"])
        rate = int(device_info["defaultSampleRate"])

        wav_file = wave.open(str(path), "wb")
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(rate)

        def run() -> None:
            stream = self._pyaudio.open(
                format=pyaudio_module.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device_info["index"],
                frames_per_buffer=CHUNK_FRAMES,
            )
            try:
                while not self._stop_event.is_set():
                    data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                    wav_file.writeframes(data)
                    setattr(self, level_attr, _pcm16_level(data))
            finally:
                stream.stop_stream()
                stream.close()
                wav_file.close()
                setattr(self, level_attr, 0.0)

        thread = threading.Thread(target=run, daemon=True, name=f"recorder-{path.stem}")
        thread.start()
        return thread
