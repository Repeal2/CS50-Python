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

CHUNK_FRAMES = 1024
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM


@dataclass(frozen=True)
class RecordedAudio:
    mic_path: Path
    system_path: Path
    started_at_monotonic: float


class Recorder:
    """Records the default microphone and the default output device's loopback to two WAV files.

    The two streams are kept in separate files (rather than mixed) so the transcription engine can
    label segments by source ("mic" vs. "system") when it merges them into the meeting transcript.
    """

    def __init__(self, output_dir: Path):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._pyaudio = None
        self._mic_thread: threading.Thread | None = None
        self._system_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at: float | None = None
        self._mic_path = self._output_dir / "mic.wav"
        self._system_path = self._output_dir / "system.wav"

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

        mic_info = self._pyaudio.get_default_input_device_info()
        system_info = self._get_default_loopback_device(pyaudio)

        self._mic_thread = self._spawn_capture_thread(pyaudio, mic_info, self._mic_path)
        self._system_thread = self._spawn_capture_thread(pyaudio, system_info, self._system_path)

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

    def _spawn_capture_thread(self, pyaudio_module, device_info: dict, path: Path) -> threading.Thread:
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
            finally:
                stream.stop_stream()
                stream.close()
                wav_file.close()

        thread = threading.Thread(target=run, daemon=True, name=f"recorder-{path.stem}")
        thread.start()
        return thread
