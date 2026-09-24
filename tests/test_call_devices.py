"""Finding the microphone and speaker Teams is using, from Windows' audio sessions (see audio.call_devices)."""

from meeting_scribe.audio.call_devices import (
    SESSION_ACTIVE,
    SESSION_INACTIVE,
    AudioSessionInfo,
    CallAudioDevices,
    call_audio_devices_from,
    find_call_audio_devices,
    is_call_app_process,
    match_portaudio_device,
)

OWN_PID = 999
PROCESSES = {
    1: ("explorer.exe", 0),
    10: ("ms-teams.exe", 1),
    11: ("msedgewebview2.exe", 10),  # new Teams does some of its work in WebView2 children
    20: ("chrome.exe", 1),
    OWN_PID: ("MeetingScribe.exe", 1),
}

HEADSET_MIC = "Headset Microphone (Jabra Evolve2 65)"
LAPTOP_MIC = "Microphone Array (Realtek(R) Audio)"
HEADSET_OUT = "Headphones (Jabra Evolve2 65)"
SPEAKERS = "Speakers (Realtek(R) Audio)"


def _session(capture, name, pid, state=SESSION_ACTIVE):
    return AudioSessionInfo(capture=capture, endpoint_name=name, pid=pid, state=state)


def test_teams_is_recognized_directly_and_through_its_child_processes():
    assert is_call_app_process(10, PROCESSES)
    assert is_call_app_process(11, PROCESSES)
    assert not is_call_app_process(20, PROCESSES)
    assert not is_call_app_process(12345, PROCESSES)  # exited since


def test_a_parent_cycle_does_not_hang():
    assert not is_call_app_process(1, {1: ("a.exe", 2), 2: ("b.exe", 1)})


def test_the_devices_teams_has_active_sessions_on_are_the_ones_in_use():
    sessions = [
        _session(True, LAPTOP_MIC, 10, SESSION_INACTIVE),  # used earlier, not now
        _session(True, LAPTOP_MIC, OWN_PID),  # this app's own recording
        _session(True, HEADSET_MIC, 11),
        _session(False, SPEAKERS, 20),  # a browser playing something
        _session(False, HEADSET_OUT, 10),
    ]
    assert call_audio_devices_from(sessions, PROCESSES, OWN_PID) == CallAudioDevices((HEADSET_MIC,), (HEADSET_OUT,))


def test_nothing_is_reported_when_teams_is_not_using_audio():
    sessions = [_session(True, LAPTOP_MIC, OWN_PID), _session(False, SPEAKERS, 20)]
    assert call_audio_devices_from(sessions, PROCESSES, OWN_PID) is None


def test_one_side_can_be_known_without_the_other():
    sessions = [_session(False, SPEAKERS, 10)]
    assert call_audio_devices_from(sessions, PROCESSES, OWN_PID) == CallAudioDevices((), (SPEAKERS,))


def test_the_full_wasapi_name_is_preferred_over_the_truncated_mme_one():
    names = ["Headset Microphone (Jabra Evol", HEADSET_MIC, LAPTOP_MIC]
    assert match_portaudio_device(HEADSET_MIC, names) == HEADSET_MIC
    assert match_portaudio_device(HEADSET_MIC, ["Headset Microphone (Jabra Evol"]) == "Headset Microphone (Jabra Evol"
    assert match_portaudio_device(HEADSET_MIC, [LAPTOP_MIC]) is None


def test_an_output_is_matched_to_its_loopback_device():
    names = [f"{SPEAKERS} [Loopback]", f"{HEADSET_OUT} [Loopback]"]
    assert match_portaudio_device(HEADSET_OUT, names) == f"{HEADSET_OUT} [Loopback]"


def test_a_failure_asking_windows_means_cannot_tell():
    def broken():
        raise OSError("COM said no")

    assert find_call_audio_devices(sessions=broken, processes=lambda: PROCESSES) is None
    assert find_call_audio_devices(
        sessions=lambda: [_session(True, HEADSET_MIC, 10)], processes=lambda: PROCESSES
    ) == CallAudioDevices((HEADSET_MIC,), ())


def test_every_device_teams_has_open_is_reported_once():
    sessions = [_session(True, HEADSET_MIC, 10), _session(True, LAPTOP_MIC, 11), _session(True, HEADSET_MIC, 11)]
    assert call_audio_devices_from(sessions, PROCESSES, OWN_PID).microphones == (HEADSET_MIC, LAPTOP_MIC)
