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
        return input_devices_from(p)
    finally:
        _close_pyaudio(p)


def list_loopback_devices() -> list[AudioDevice]:
    """Speaker/output devices whose WASAPI loopback can be recorded as "system audio"."""
    if sys.platform != "win32":
        raise RuntimeError("Device selection requires Windows (PyAudioWPatch)")
    import pyaudiowpatch as pyaudio

    p = _open_pyaudio(pyaudio)
    try:
        return loopback_devices_from(p)
    finally:
        _close_pyaudio(p)


# PortAudio's id for the WASAPI host API (PaHostApiTypeId's paWASAPI) — the same value as
# pyaudiowpatch.paWASAPI, spelled out so enumerating doesn't need the module as well as an instance.
PA_WASAPI = 13


def _wasapi_host_api(pyaudio_instance) -> dict | None:
    try:
        return pyaudio_instance.get_host_api_info_by_type(PA_WASAPI)
    except Exception:  # no WASAPI on this machine (or a stand-in that doesn't model host APIs)
        return None


def input_device_infos(pyaudio_instance) -> list[dict]:
    """Every microphone, once each — the WASAPI copy of it.

    Windows lists each device once per audio API PortAudio knows (MME, DirectSound, WASAPI, WDM-KS), and
    MME, which comes first, cuts names off at 31 characters. Picking a device by name from all of them
    used to open whichever copy came first — MME's, which runs through Windows' legacy audio mapper at
    its own sample rate, or WDM-KS's, which talks to the driver directly and can take the device away
    from Teams. WASAPI is what Teams itself and the system-audio loopback both use: full names that
    match what Windows (and audio.call_devices) report, at the device's own rate. Falls back to every
    input only if there's no WASAPI at all."""
    infos = [
        info
        for info in (
            pyaudio_instance.get_device_info_by_index(i) for i in range(pyaudio_instance.get_device_count())
        )
        if info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice")
    ]
    host = _wasapi_host_api(pyaudio_instance)
    if host is not None and "index" in host:
        wasapi = [info for info in infos if info.get("hostApi") == host["index"]]
        if wasapi:
            return wasapi
    return infos


def default_input_device_info(pyaudio_instance) -> dict:
    """The Windows default microphone, as its WASAPI device — see input_device_infos. PortAudio's own
    default input is MME's."""
    host = _wasapi_host_api(pyaudio_instance)
    index = host.get("defaultInputDevice", -1) if host is not None else -1
    if isinstance(index, int) and index >= 0:
        try:
            return pyaudio_instance.get_device_info_by_index(index)
        except Exception:
            pass
    return pyaudio_instance.get_default_input_device_info()


def full_device_name(saved: str, device_names) -> str:
    """`saved` as the device it names is listed now: a name saved from MME's list by an older version is
    cut off at 31 characters, and becomes the one device whose full name it begins. Anything else —
    already full, unplugged, or ambiguous — comes back unchanged."""
    names = list(device_names)
    if not saved or saved in names or len(saved) < 16:
        return saved
    matches = [name for name in names if name.startswith(saved)]
    return matches[0] if len(matches) == 1 else saved


def input_devices_from(pyaudio_instance) -> list[AudioDevice]:
    """list_input_devices' enumeration on an already-open PyAudio instance — which is the only way to
    see a current list while a meeting is recording: a fresh PyAudio() then just shares the Recorder's
    snapshot (see audio.device_watch), so the list has to come from the Recorder itself after it
    reloads (see Recorder.available_devices). One entry per microphone — see input_device_infos."""
    return [AudioDevice(index=info["index"], name=info["name"]) for info in input_device_infos(pyaudio_instance)]


def loopback_devices_from(pyaudio_instance) -> list[AudioDevice]:
    """list_loopback_devices' enumeration on an already-open PyAudio instance — see input_devices_from."""
    return [
        AudioDevice(index=info["index"], name=info["name"])
        for info in pyaudio_instance.get_loopback_device_info_generator()
    ]
