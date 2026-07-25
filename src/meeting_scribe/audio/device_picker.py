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


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str


def list_input_devices() -> list[AudioDevice]:
    """Microphones and other recording devices (excludes WASAPI loopback pseudo-devices)."""
    if sys.platform != "win32":
        raise RuntimeError("Device selection requires Windows (PyAudioWPatch)")
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        return [
            AudioDevice(index=info["index"], name=info["name"])
            for info in (p.get_device_info_by_index(i) for i in range(p.get_device_count()))
            if info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice")
        ]
    finally:
        p.terminate()


def list_loopback_devices() -> list[AudioDevice]:
    """Speaker/output devices whose WASAPI loopback can be recorded as "system audio"."""
    if sys.platform != "win32":
        raise RuntimeError("Device selection requires Windows (PyAudioWPatch)")
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        return [
            AudioDevice(index=info["index"], name=info["name"])
            for info in p.get_loopback_device_info_generator()
        ]
    finally:
        p.terminate()
