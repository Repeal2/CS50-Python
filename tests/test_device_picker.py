import sys

import pytest

from meeting_scribe.audio.device_picker import AudioDevice, list_input_devices, list_loopback_devices

# Mirrors screen.window_picker's tests: the platform guard only fires off Windows — on the real
# windows-latest CI runner, PyAudioWPatch actually works, so exercise real behavior there instead.


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_list_input_devices_requires_windows():
    with pytest.raises(RuntimeError):
        list_input_devices()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_list_loopback_devices_requires_windows():
    with pytest.raises(RuntimeError):
        list_loopback_devices()


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real PyAudioWPatch calls")
def test_list_input_devices_returns_audio_devices_on_windows():
    devices = list_input_devices()
    assert isinstance(devices, list)
    assert all(isinstance(d, AudioDevice) for d in devices)


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real PyAudioWPatch calls")
def test_list_loopback_devices_returns_audio_devices_on_windows():
    devices = list_loopback_devices()
    assert isinstance(devices, list)
    assert all(isinstance(d, AudioDevice) for d in devices)
