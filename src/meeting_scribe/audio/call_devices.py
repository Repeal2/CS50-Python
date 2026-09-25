"""Which microphone and speaker the call app (Teams) is actually using right now.

Everything else in audio.recorder that picks a device does it from the outside: it guesses from device
names ("Headset Microphone (…)") and from which device happens to be making noise. Windows can just be
asked. Every app that opens an audio device gets an *audio session* on that device's endpoint — the same
sessions the Volume Mixer lists, one per app per device — and each session knows the process that owns
it and whether its stream is running. So "the endpoint where a Teams process has an active capture
session" is the microphone Teams is using, whatever it's called and whether or not it's the Windows
default; the same on the render side is where the call's sound is coming out. That's the question the
user actually wants answered, and it needs no guessing.

Teams keeps its microphone session active while muted, so a muted call still answers correctly. Our own
process is excluded — the recorder has sessions of its own on the same devices.

Windows-only (Core Audio over COM, via pycaw/comtypes, and psutil for the process tree). Anywhere else,
or if anything in that chain fails, find_call_audio_devices() returns None — "can't tell" — and the
recorder carries on with its name/level heuristics.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

# Process names (lower-cased, matched anywhere in the name) that count as the call app: the new Teams
# client (ms-teams.exe, and its ms-teams_modulehost.exe helper), classic Teams (Teams.exe).
CALL_APP_HINTS = ("teams",)

# How far up the process tree to look for a call app. The new Teams client does some of its work in
# WebView2 (msedgewebview2.exe) child processes; the session belongs to the child, and the parent is
# what says it's Teams.
_MAX_ANCESTRY = 6

# AudioSessionState — Windows' own values.
SESSION_INACTIVE = 0
SESSION_ACTIVE = 1

# The suffix PyAudioWPatch gives each output device's loopback twin.
_LOOPBACK_SUFFIX = " [Loopback]"


@dataclass(frozen=True)
class AudioSessionInfo:
    """One app's audio session on one endpoint, as Windows reports it."""

    capture: bool  # True for a microphone (capture endpoint), False for a speaker (render endpoint)
    endpoint_name: str  # the endpoint's friendly name, e.g. "Headset Microphone (Jabra Evolve2 65)"
    pid: int
    state: int  # SESSION_INACTIVE / SESSION_ACTIVE / expired


@dataclass(frozen=True)
class CallAudioDevices:
    """The endpoints the call app has active sessions on, in the order Windows lists them — empty for a
    side it isn't using right now. Usually one each; Teams can hold a second one open (a separate ringer
    device, say), which is why the recorder stays on any of them rather than always taking the first."""

    microphones: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()
    # Set only when the call app has more than one microphone open and one of them is Windows' default
    # communications microphone — the tie-breaker between them.
    default_microphone: str | None = None


def is_call_app_process(pid: int, processes: Mapping[int, tuple[str, int]]) -> bool:
    """Whether `pid`, or one of its ancestors, is the call app. `processes` maps pid -> (exe name,
    parent pid). A cycle or a missing parent just ends the walk."""
    seen = set()
    current = pid
    for _ in range(_MAX_ANCESTRY):
        if current in seen or current not in processes:
            return False
        seen.add(current)
        name, parent = processes[current]
        if any(hint in name.lower() for hint in CALL_APP_HINTS):
            return True
        current = parent
    return False


def call_audio_devices_from(
    sessions: Iterable[AudioSessionInfo],
    processes: Mapping[int, tuple[str, int]],
    own_pid: int,
    default_microphone: str | None = None,
) -> CallAudioDevices | None:
    """The pure half of find_call_audio_devices: which endpoints the call app has *active* sessions on.
    An inactive session is an app that opened the device at some point but isn't streaming now (Teams
    keeps idle ones around for its ringer and for devices it used earlier), so only an active one says
    where the call is. None when the call app isn't using either side."""
    microphones: list[str] = []
    speakers: list[str] = []
    for session in sessions:
        if session.state != SESSION_ACTIVE or session.pid in (0, own_pid):
            continue
        if not is_call_app_process(session.pid, processes):
            continue
        found = microphones if session.capture else speakers
        if session.endpoint_name not in found:
            found.append(session.endpoint_name)
    if not microphones and not speakers:
        return None
    return CallAudioDevices(
        microphones=tuple(microphones),
        speakers=tuple(speakers),
        default_microphone=default_microphone if len(microphones) > 1 and default_microphone in microphones else None,
    )


def _same_endpoint(portaudio_name: str, endpoint_name: str) -> bool:
    """Whether a PortAudio device name refers to this endpoint. PortAudio's WASAPI names are the
    endpoint's friendly name exactly; its MME names are the same cut off at 31 characters; and
    PyAudioWPatch's loopback devices add " [Loopback]"."""
    name = portaudio_name
    if name.endswith(_LOOPBACK_SUFFIX):
        name = name[: -len(_LOOPBACK_SUFFIX)]
    if name == endpoint_name:
        return True
    shorter, longer = sorted((name, endpoint_name), key=len)
    return len(shorter) >= 16 and longer.startswith(shorter)


def match_portaudio_device(endpoint_name: str, portaudio_names: Iterable[str]) -> str | None:
    """The PortAudio device name for an endpoint: an exact match first (the WASAPI one, which carries
    the full name), otherwise one that's the same device under a truncated name."""
    names = list(portaudio_names)
    for name in names:
        stripped = name[: -len(_LOOPBACK_SUFFIX)] if name.endswith(_LOOPBACK_SUFFIX) else name
        if stripped == endpoint_name:
            return name
    for name in names:
        if _same_endpoint(name, endpoint_name):
            return name
    return None


# --- the Windows side ------------------------------------------------------------------------------------

_com_ready = threading.local()


def _ensure_com() -> None:
    """COM has to be initialized on each thread that uses it; the recorder asks from its own background
    thread. Done once per thread and never undone — the COM objects created here are released by
    garbage collection, which may happen after any explicit uninitialize would have run."""
    if getattr(_com_ready, "done", False):
        return
    import comtypes

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except Exception:
        pass  # already initialized on this thread, in the other threading model — still usable
    _com_ready.done = True


def _windows_default_microphone() -> str | None:
    """The friendly name of Windows' default communications microphone (eCapture, eCommunications) —
    what a call app's "default" microphone setting follows — or None if there isn't one."""
    import comtypes
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import AudioUtilities

    _ensure_com()
    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
    )
    try:
        endpoint = enumerator.GetDefaultAudioEndpoint(1, 2)  # eCapture, eCommunications
        return str(AudioUtilities.CreateDevice(endpoint).FriendlyName) or None
    except Exception:  # no capture device at all
        return None


def _windows_sessions() -> list[AudioSessionInfo]:
    import comtypes
    from pycaw.api.audiopolicy import IAudioSessionControl2
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import AudioUtilities

    _ensure_com()
    enumerator = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
    )
    sessions: list[AudioSessionInfo] = []
    for flow, capture in ((0, False), (1, True)):  # eRender, eCapture
        collection = enumerator.EnumAudioEndpoints(flow, 0x1)  # DEVICE_STATE_ACTIVE
        for index in range(collection.GetCount()):
            try:
                device = AudioUtilities.CreateDevice(collection.Item(index))
                name = device.FriendlyName
                session_enumerator = device.AudioSessionManager.GetSessionEnumerator()
                count = session_enumerator.GetCount()
            except Exception:  # one endpoint that won't answer shouldn't hide the rest
                continue
            if not name:
                continue
            for session_index in range(count):
                try:
                    control = session_enumerator.GetSession(session_index)
                    control2 = control.QueryInterface(IAudioSessionControl2)
                    sessions.append(
                        AudioSessionInfo(
                            capture=capture,
                            endpoint_name=str(name),
                            pid=int(control2.GetProcessId()),
                            state=int(control2.GetState()),
                        )
                    )
                except Exception:
                    continue
    return sessions


class _LiveProcesses(Mapping):
    """pid -> (exe name, parent pid), looked up only for the processes actually asked about — the few
    that own audio sessions, and their parents — rather than listing every process on the machine each
    time the recorder asks."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple[str, int] | None] = {}

    def _lookup(self, pid: int) -> tuple[str, int] | None:
        if pid not in self._cache:
            import psutil

            try:
                process = psutil.Process(pid)
                self._cache[pid] = (process.name(), process.ppid())
            except Exception:  # exited, or protected
                self._cache[pid] = None
        return self._cache[pid]

    def __contains__(self, pid: object) -> bool:
        return isinstance(pid, int) and self._lookup(pid) is not None

    def __getitem__(self, pid: int) -> tuple[str, int]:
        found = self._lookup(pid)
        if found is None:
            raise KeyError(pid)
        return found

    def __iter__(self):
        return iter(pid for pid, found in self._cache.items() if found is not None)

    def __len__(self) -> int:
        return sum(1 for found in self._cache.values() if found is not None)


def _windows_processes() -> Mapping[int, tuple[str, int]]:
    return _LiveProcesses()


def find_call_audio_devices(
    sessions: Callable[[], Iterable[AudioSessionInfo]] | None = None,
    processes: Callable[[], Mapping[int, tuple[str, int]]] | None = None,
    default_microphone: Callable[[], str | None] | None = None,
) -> CallAudioDevices | None:
    """The microphone and speaker Teams is using right now (see CallAudioDevices), or None if Teams
    isn't using any audio device, this isn't Windows, or Windows couldn't be asked. Never raises: it's
    polled from a background thread during a meeting, and a failed poll just means "can't tell"."""
    if sessions is None or processes is None:
        if sys.platform != "win32":
            return None
        sessions = sessions or _windows_sessions
        processes = processes or _windows_processes
        default_microphone = default_microphone or _windows_default_microphone
    try:
        current_sessions, current_processes = list(sessions()), processes()
        found = call_audio_devices_from(current_sessions, current_processes, os.getpid())
        if found is not None and len(found.microphones) > 1 and default_microphone is not None:
            # Only asked when it can decide something: Teams with more than one microphone open.
            found = call_audio_devices_from(current_sessions, current_processes, os.getpid(), default_microphone())
        return found
    except Exception:
        return None
