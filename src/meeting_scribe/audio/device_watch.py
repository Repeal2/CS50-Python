"""Cheap detection of "the set of audio devices, or which one is the Windows default, just changed".

PortAudio (under PyAudioWPatch) can't answer that: it takes one snapshot of the device list — defaults
included — when it's initialized and never updates it, and the snapshot is only retaken once *every*
PyAudio instance in the process has been terminated. So a headset plugged in mid-meeting, while the
Recorder holds PortAudio open, is invisible to anything asked through PortAudio. The legacy winmm API
isn't cached that way (on Vista and later it sits on top of the live Core Audio device list), and its
calls here are plain queries — no callbacks, no windows, no COM — so polling it every few seconds is
cheap and safe. It only says *that* something changed; the Recorder then restarts PortAudio to find out
what (see Recorder.reload_devices).

Windows-only; device_signature() returns None anywhere else, or if any winmm call fails, which callers
treat as "can't tell" rather than "changed".
"""

from __future__ import annotations

import sys

# Documented in mmddk.h: asks the wave mapper which device it currently prefers — the Windows default.
_DRVM_MAPPER_PREFERRED_GET = 0x2015
# WAVE_MAPPER is (UINT)-1; as a handle it's that value zero-extended, not sign-extended.
_WAVE_MAPPER_HANDLE = 0xFFFFFFFF
_MAXPNAMELEN = 32


def device_signature() -> tuple | None:
    """A value that changes whenever an audio input/output device is added or removed, or the default
    one changes: the device counts, every device's name, and the preferred (default) device ids."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _WaveInCaps(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD),
            ("wPid", wintypes.WORD),
            ("vDriverVersion", wintypes.UINT),
            ("szPname", wintypes.WCHAR * _MAXPNAMELEN),
            ("dwFormats", wintypes.DWORD),
            ("wChannels", wintypes.WORD),
            ("wReserved1", wintypes.WORD),
        ]

    class _WaveOutCaps(ctypes.Structure):
        _fields_ = _WaveInCaps._fields_ + [("dwSupport", wintypes.DWORD)]

    try:
        winmm = ctypes.windll.winmm
        signature: list = []
        for get_count, get_caps, caps_type, message in (
            (winmm.waveInGetNumDevs, winmm.waveInGetDevCapsW, _WaveInCaps, winmm.waveInMessage),
            (winmm.waveOutGetNumDevs, winmm.waveOutGetDevCapsW, _WaveOutCaps, winmm.waveOutMessage),
        ):
            get_count.restype = wintypes.UINT
            get_caps.argtypes = [ctypes.c_size_t, ctypes.c_void_p, wintypes.UINT]
            get_caps.restype = wintypes.UINT
            message.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t]
            message.restype = wintypes.UINT

            count = int(get_count())
            names = []
            for device_id in range(count):
                caps = caps_type()
                if get_caps(device_id, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                    names.append(caps.szPname)
            preferred = wintypes.DWORD(0xFFFFFFFF)
            flags = wintypes.DWORD(0)
            message(
                _WAVE_MAPPER_HANDLE,
                _DRVM_MAPPER_PREFERRED_GET,
                ctypes.addressof(preferred),
                ctypes.addressof(flags),
            )
            signature.append((count, tuple(names), int(preferred.value)))
        return tuple(signature)
    except (AttributeError, OSError, ValueError):
        return None
