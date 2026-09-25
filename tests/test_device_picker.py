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


class _MultiApiHost:
    """Enough of PyAudio to list one headset and one laptop mic the way Windows does: once per audio API
    (MME first, with its 31-character names; then WASAPI; then WDM-KS), plus a WASAPI loopback."""

    MME, WASAPI, WDMKS = 0, 2, 3
    HEADSET = "Headset Microphone (Jabra Evolve2 65)"
    LAPTOP = "Microphone Array (Intel Smart Sound Technology)"

    def __init__(self, default_wasapi_input=4):
        self._default_wasapi_input = default_wasapi_input
        self._devices = [
            self._device(0, self.HEADSET[:31], self.MME),
            self._device(1, self.LAPTOP[:31], self.MME),
            self._device(2, "Speakers (Realtek)", self.WASAPI, inputs=0),
            self._device(3, self.HEADSET, self.WASAPI),
            self._device(4, self.LAPTOP, self.WASAPI),
            self._device(5, "Speakers (Realtek) [Loopback]", self.WASAPI, loopback=True),
            self._device(6, self.HEADSET, self.WDMKS),
        ]

    @staticmethod
    def _device(index, name, host_api, inputs=1, loopback=False):
        return {"index": index, "name": name, "hostApi": host_api, "maxInputChannels": inputs,
                "defaultSampleRate": 48000, "isLoopbackDevice": loopback}

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, index):
        return self._devices[index]

    def get_host_api_info_by_type(self, host_api_type):
        assert host_api_type == 13  # paWASAPI
        return {"index": self.WASAPI, "defaultInputDevice": self._default_wasapi_input}

    def get_default_input_device_info(self):
        return self._devices[1]  # PortAudio's own default is MME's


def test_each_microphone_is_listed_once_as_its_wasapi_device():
    from meeting_scribe.audio.device_picker import input_device_infos, input_devices_from

    host = _MultiApiHost()

    assert [(info["index"], info["name"]) for info in input_device_infos(host)] == [
        (3, _MultiApiHost.HEADSET),
        (4, _MultiApiHost.LAPTOP),
    ]
    assert [device.name for device in input_devices_from(host)] == [_MultiApiHost.HEADSET, _MultiApiHost.LAPTOP]


def test_the_default_microphone_is_the_wasapi_default_not_mmes():
    from meeting_scribe.audio.device_picker import default_input_device_info

    assert default_input_device_info(_MultiApiHost())["index"] == 4
    # No WASAPI default (no input at all): PortAudio's own answer is all there is.
    assert default_input_device_info(_MultiApiHost(default_wasapi_input=-1))["index"] == 1


def test_a_microphone_is_opened_through_wasapi_even_when_saved_under_mmes_cut_off_name():
    from meeting_scribe.audio.recorder import _find_input_device

    host = _MultiApiHost()

    assert _find_input_device(host, _MultiApiHost.HEADSET)["index"] == 3
    assert _find_input_device(host, _MultiApiHost.HEADSET[:31])["index"] == 3
    assert _find_input_device(host, "Something else entirely") is None


def test_a_saved_name_cut_off_by_mme_becomes_the_full_name():
    from meeting_scribe.audio.device_picker import full_device_name

    names = [_MultiApiHost.HEADSET, _MultiApiHost.LAPTOP]
    assert full_device_name(_MultiApiHost.HEADSET[:31], names) == _MultiApiHost.HEADSET
    assert full_device_name(_MultiApiHost.LAPTOP, names) == _MultiApiHost.LAPTOP
    assert full_device_name("Unplugged Headset Microphone", names) == "Unplugged Headset Microphone"
    # Too short to be sure, or matching more than one: left alone.
    assert full_device_name("Headset", names) == "Headset"
    assert full_device_name("Microphone Array (Intel", names + ["Microphone Array (Intel HD)"]) == (
        "Microphone Array (Intel"
    )
