"""Enumerates microphone and system-output devices so the user can pick which one to record, instead of
always using whatever Windows currently considers the default — and so the app can show the user which
device it's actually using.

Windows-only (wraps PyAudioWPatch, the same PyAudio fork audio.recorder uses for WASAPI loopback).
Import is deferred so this module can be imported anywhere; calling either function off Windows raises
RuntimeError, matching audio.recorder's and screen.window_picker's pattern.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


def _open_pyaudio(pyaudio_module):
    """PyAudio construction and teardown touch PortAudio's global (unlocked) init state, so they share
    the recorder's lifecycle lock — "Refresh devices" can otherwise land at the same moment as a
    finishing meeting's Recorder.stop() tearing PortAudio down on its background thread."""
    from meeting_scribe.audio.recorder import _pyaudio_lifecycle_lock

    with _pyaudio_lifecycle_lock:
        return pyaudio_module.PyAudio()


def _close_pyaudio(pyaudio_instance) -> None:
    from meeting_scribe.audio.recorder import _pyaudio_lifecycle_lock

    with _pyaudio_lifecycle_lock:
        pyaudio_instance.terminate()


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str


def list_input_devices() -> list[AudioDevice]:
    """Microphones and other recording devices (excludes WASAPI loopback pseudo-devices)."""
    if sys.platform != "win32":
        raise RuntimeError("Device selection requires Windows (PyAudioWPatch)")
    import pyaudiowpatch as pyaudio

    p = _open_pyaudio(pyaudio)
    try:
        return [
            AudioDevice(index=info["index"], name=info["name"])
            for info in (p.get_device_info_by_index(i) for i in range(p.get_device_count()))
            if info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice")
        ]
    finally:
        _close_pyaudio(p)


def list_loopback_devices() -> list[AudioDevice]:
    """Speaker/output devices whose WASAPI loopback can be recorded as "system audio"."""
    if sys.platform != "win32":
        raise RuntimeError("Device selection requires Windows (PyAudioWPatch)")
    import pyaudiowpatch as pyaudio

    p = _open_pyaudio(pyaudio)
    try:
        return [
            AudioDevice(index=info["index"], name=info["name"])
            for info in p.get_loopback_device_info_generator()
        ]
    finally:
        _close_pyaudio(p)
