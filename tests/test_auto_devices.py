"""Automatic device selection: system audio follows whichever output is playing, and the microphone is
the headset whenever it's connected and live (see audio.recorder)."""

import contextlib
import struct
import threading
import time
import wave
from types import SimpleNamespace

from meeting_scribe.audio.recorder import (
    SAMPLE_WIDTH_BYTES,
    DeviceProbe,
    Recorder,
    _HEADSET_RECHECK_SECONDS,
    _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS,
    _SegmentedWavWriter,
    _SILENT_DBFS,
    _StreamActivityMonitor,
    _SYSTEM_QUIET_BEFORE_SEARCH_SECONDS,
    _analyze_pcm16,
    _probe_devices,
    choose_fallback_microphone,
    find_headset_microphones,
    looks_like_headset,
    next_microphone,
    next_system_device,
)


def _pcm16(value, count=512):
    return struct.pack(f"<{count}h", *([value] * count))


LOUD = _pcm16(8000)  # about -12 dBFS: something clearly playing
QUIET = _pcm16(3)  # a live device's noise floor: non-zero, but nothing anyone would hear
SILENT = _pcm16(0)  # digital silence


# --- what the monitor measures ------------------------------------------------------------------------


def test_time_without_sound_and_without_signal_are_measured_separately():
    monitor = _StreamActivityMonitor()
    monitor.stream_opened(0.0)
    monitor.observe(_analyze_pcm16(QUIET), 1.0)

    health = monitor.health(5.0)
    assert health.seconds_without_sound == 5.0  # never audible: counted from when it opened
    assert health.seconds_without_signal == 4.0

    monitor.observe(_analyze_pcm16(LOUD), 6.0)
    assert monitor.health(7.0).seconds_without_sound == 1.0


def test_a_switch_restarts_the_count_rather_than_crediting_the_previous_device():
    monitor = _StreamActivityMonitor()
    monitor.stream_opened(0.0)
    monitor.observe(_analyze_pcm16(LOUD), 1.0)
    monitor.stream_opened(10.0)

    health = monitor.health(12.0)
    assert (health.seconds_without_sound, health.seconds_without_signal) == (2.0, 2.0)


def test_a_stream_that_never_opened_reports_no_time_without_anything():
    health = _StreamActivityMonitor().health(100.0)
    assert (health.seconds_without_sound, health.seconds_without_signal) == (None, None)


# --- choosing the system-audio device -------------------------------------------------------------------


def _probe(name, peak_dbfs):
    return DeviceProbe(name=name, peak_dbfs=peak_dbfs, heard_signal=peak_dbfs > _SILENT_DBFS)


def test_system_audio_stays_put_while_the_recorded_device_has_been_quiet_only_briefly():
    choice = next_system_device(
        active="Speakers",
        active_seconds_without_sound=_SYSTEM_QUIET_BEFORE_SEARCH_SECONDS - 1,
        probes=[_probe("Headset", -20.0)],
    )
    assert choice is None


def test_system_audio_follows_the_output_that_is_playing():
    choice = next_system_device(
        active="Speakers",
        active_seconds_without_sound=_SYSTEM_QUIET_BEFORE_SEARCH_SECONDS,
        probes=[_probe("Monitor", _SILENT_DBFS), _probe("Headset", -25.0), _probe("Dock", -40.0)],
    )
    assert choice == "Headset"


def test_system_audio_does_not_chase_a_device_that_is_barely_making_a_sound():
    choice = next_system_device(
        active="Speakers",
        active_seconds_without_sound=60.0,
        probes=[_probe("Headset", -70.0), _probe("Speakers", -10.0)],
    )
    assert choice is None


# --- choosing the microphone ------------------------------------------------------------------------------


INPUTS = [
    "Microphone Array (Realtek)",
    "Stereo Mix (Realtek)",
    "Headset Microphone (Jabra Evol",  # the same headset as it appears under MME, cut to 31 characters
    "Headset Microphone (Jabra Evolve2 65)",
]


def test_headsets_are_recognized_by_the_names_windows_gives_them():
    assert looks_like_headset("Headset Microphone (Jabra Evolve2 65)")
    assert looks_like_headset("Headset (WH-1000XM4 Hands-Free AG Audio)")
    assert not looks_like_headset("Microphone Array (Realtek(R) Audio)")


def test_every_connected_headset_is_found_once_with_the_one_from_settings_first():
    names = INPUTS + ["USB Audio Device", "Headset (WH-1000XM4 Hands-Free AG Audio)"]
    assert find_headset_microphones(names, configured="USB Audio Device") == [
        "USB Audio Device",
        "Headset Microphone (Jabra Evol",  # the full-length Jabra name is the same headset
        "Headset (WH-1000XM4 Hands-Free AG Audio)",
    ]


def test_a_headset_picked_in_settings_that_is_not_connected_is_left_out():
    assert find_headset_microphones(INPUTS, configured="USB Audio Device") == [
        "Headset Microphone (Jabra Evol"
    ]


def test_no_headsets_connected():
    assert find_headset_microphones(["Microphone Array (Realtek)"], configured=None) == []


def test_the_fallback_is_the_microphone_chosen_in_settings():
    fallback = choose_fallback_microphone(
        INPUTS, configured="Microphone Array (Realtek)", default_name=None, headsets=[INPUTS[3]]
    )
    assert fallback == "Microphone Array (Realtek)"


def test_the_fallback_is_never_a_headset_even_when_windows_made_it_the_default():
    fallback = choose_fallback_microphone(
        INPUTS + ["USB Audio Device"], configured=INPUTS[3], default_name="USB Audio Device",
        headsets=["USB Audio Device", INPUTS[3]],
    )
    assert fallback == "Microphone Array (Realtek)"


def test_the_fallback_skips_loopback_inputs():
    fallback = choose_fallback_microphone(
        ["Stereo Mix (Realtek)", "Headset Microphone (Jabra)", "Microphone (USB)"],
        configured=None,
        default_name=None,
        headsets=["Headset Microphone (Jabra)"],
    )
    assert fallback == "Microphone (USB)"


HEADSETS = ["Jabra", "Sony"]


def _next(active, *, dead=False, live=None, fallback="Laptop"):
    return next_microphone(
        active=active, headsets=HEADSETS, fallback=fallback, active_is_dead=dead, live_headsets=live
    )


def test_a_working_headset_is_kept():
    assert _next("Jabra") is None
    assert _next("Jabra", live=["Sony"]) is None


def test_a_dead_headset_hands_over_to_another_headset_that_is_live():
    assert _next("Jabra", dead=True, live=["Sony"]) == "Sony"


def test_the_laptop_is_used_only_when_no_headset_is_live():
    assert _next("Jabra", dead=True, live=[]) == "Laptop"
    assert _next("Jabra", dead=True, live=["Jabra"]) == "Laptop"  # itself doesn't count as another


def test_from_the_laptop_the_first_live_headset_takes_over():
    assert _next("Laptop", live=["Jabra", "Sony"]) == "Jabra"
    assert _next("Laptop", live=["Sony"]) == "Sony"
    assert _next("Laptop", live=[]) is None
    assert _next("Laptop", live=None) is None  # nothing was listened to this time


def test_the_same_headset_under_a_shortened_name_counts_as_the_one_in_use():
    assert next_microphone(
        active="Headset Microphone (Jabra Evol", headsets=["Headset Microphone (Jabra Evolve2 65)"],
        fallback="Laptop", active_is_dead=False, live_headsets=["Headset Microphone (Jabra Evolve2 65)"],
    ) is None


def test_nothing_changes_without_a_headset_connected():
    assert next_microphone(
        active="Laptop", headsets=[], fallback="Laptop", active_is_dead=True, live_headsets=None
    ) is None


# --- listening to devices ---------------------------------------------------------------------------------


class _PolledStream:
    """A stream that says what it has buffered, like a real PyAudio stream — `chunks` is handed out one
    per read, and an empty list means nothing is ever available (a loopback device playing nothing)."""

    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self.closed = False

    def get_read_available(self):
        return len(self._chunks[0]) // SAMPLE_WIDTH_BYTES if self._chunks else 0

    def read(self, frames, exception_on_overflow=False):
        return self._chunks.pop(0)

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class _ProbeHost:
    def __init__(self, streams, inputs=(), loopbacks=(), default_input=None):
        self.streams = streams  # device index -> stream, or an exception to raise from open()
        self.inputs = list(inputs)
        self.loopbacks = list(loopbacks)
        self.default_input = default_input

    def open(self, **kwargs):
        stream = self.streams[kwargs["input_device_index"]]
        if isinstance(stream, Exception):
            raise stream
        return stream

    def get_device_count(self):
        return len(self.inputs)

    def get_device_info_by_index(self, index):
        return self.inputs[index]

    def get_default_input_device_info(self):
        return self.default_input

    def get_loopback_device_info_generator(self):
        return iter(self.loopbacks)


def _info(name, index, loopback=False):
    return {"name": name, "index": index, "maxInputChannels": 1, "defaultSampleRate": 16000,
            "isLoopbackDevice": loopback}


PA = SimpleNamespace(paInt16=8)


def test_a_probe_reports_what_each_device_heard_and_closes_every_stream():
    playing, silent = _PolledStream([LOUD]), _PolledStream()
    host = _ProbeHost({0: playing, 1: silent, 2: OSError("device busy")})

    probes = _probe_devices(
        host, PA, [_info("Headset", 0), _info("Speakers", 1), _info("Dock", 2)], 0.1, threading.Event()
    )

    assert [probe.name for probe in probes] == ["Headset", "Speakers", "Dock"]
    assert probes[0].heard_signal and probes[0].peak_dbfs > -20
    assert not probes[1].heard_signal and probes[1].peak_dbfs == _SILENT_DBFS
    assert not probes[2].heard_signal  # couldn't be opened: reported as having heard nothing
    assert playing.closed and silent.closed


def test_a_probe_gives_up_as_soon_as_the_recording_stops():
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    _probe_devices(_ProbeHost({0: _PolledStream()}), PA, [_info("Speakers", 0)], 30.0, stop)
    assert time.monotonic() - started < 1.0


def test_a_loopback_capture_thread_can_stop_while_nothing_is_playing(tmp_path):
    # The reason loopback streams are polled: a blocking read waits for as long as the output device
    # is silent, so the thread could never be stopped — which a switch away from it depends on.
    recorder = Recorder(tmp_path)
    recorder._pyaudio = _ProbeHost({4: _PolledStream()})
    writer = _SegmentedWavWriter(tmp_path / "system.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    thread = recorder._spawn_capture_thread(
        PA, _info("Speakers [Loopback]", 4, loopback=True), writer, "system_level",
        recorder._system_monitor, "System audio", recorder._system_stop_event,
    )
    time.sleep(0.1)
    recorder._system_stop_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert recorder.capture_notices() == ()


def test_a_loopback_capture_thread_records_what_the_stream_delivers(tmp_path):
    recorder = Recorder(tmp_path)
    stream = _PolledStream([LOUD, QUIET])
    recorder._pyaudio = _ProbeHost({4: stream})
    writer = _SegmentedWavWriter(tmp_path / "system.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    thread = recorder._spawn_capture_thread(
        PA, _info("Speakers [Loopback]", 4, loopback=True), writer, "system_level",
        recorder._system_monitor, "System audio", recorder._system_stop_event,
    )
    deadline = time.monotonic() + 2
    while stream._chunks and time.monotonic() < deadline:
        time.sleep(0.01)
    recorder._system_stop_event.set()
    thread.join(timeout=2)

    with contextlib.closing(wave.open(str(tmp_path / "system.wav"), "rb")) as wav:
        assert wav.readframes(wav.getnframes()) == LOUD + QUIET


# --- the automation itself --------------------------------------------------------------------------------


def _auto_recorder(tmp_path, host, **kwargs):
    recorder = Recorder(tmp_path, **kwargs)
    recorder._started_at = 0.0
    recorder._pyaudio = host
    recorder._pyaudio_module = PA
    recorder._call_devices = lambda: None  # not in a Teams call, whatever the machine running this is doing
    switches = []
    recorder.switch_system_device = lambda name, **kw: switches.append(("system", name, kw))
    recorder.switch_mic_device = lambda name, **kw: switches.append(("mic", name, kw))
    return recorder, switches


def test_system_audio_switches_to_the_output_that_is_playing_once_the_recorded_one_goes_quiet(tmp_path):
    host = _ProbeHost(
        {5: _PolledStream(), 6: _PolledStream([LOUD] * 50)},
        loopbacks=[_info("Speakers [Loopback]", 5, True), _info("Headset [Loopback]", 6, True)],
    )
    recorder, switches = _auto_recorder(tmp_path, host, auto_system_device=True)
    recorder._system_active_device_name = "Speakers [Loopback]"
    recorder._system_monitor.stream_opened(time.monotonic() - _SYSTEM_QUIET_BEFORE_SEARCH_SECONDS - 1)

    recorder._auto_select_system_device(time.monotonic())

    assert switches == [(
        "system", "Headset [Loopback]",
        {"reason": "the call's sound is playing through it", "automatic": True},
    )]


def test_system_audio_is_left_alone_while_it_is_playing(tmp_path):
    host = _ProbeHost({6: _PolledStream([LOUD] * 50)}, loopbacks=[_info("Headset [Loopback]", 6, True)])
    recorder, switches = _auto_recorder(tmp_path, host, auto_system_device=True)
    recorder._system_active_device_name = "Speakers [Loopback]"
    recorder._system_monitor.stream_opened(time.monotonic() - 60)
    recorder._system_monitor.observe(_analyze_pcm16(LOUD), time.monotonic())

    recorder._auto_select_system_device(time.monotonic())

    assert switches == []


LAPTOP, JABRA, SONY = "Microphone Array (Realtek)", "Headset Microphone (Jabra)", "Headset (Sony Hands-Free)"


def _mic_host(jabra_stream, sony_stream=None):
    devices = [_info(LAPTOP, 0), _info(JABRA, 1)]
    streams = {1: jabra_stream}
    if sony_stream is not None:
        devices.append(_info(SONY, 2))
        streams[2] = sony_stream
    return _ProbeHost(streams, inputs=devices, default_input=devices[1])


def _on_headset(tmp_path, host, silent_for):
    recorder, switches = _auto_recorder(tmp_path, host, auto_microphone=True, mic_device_name=LAPTOP)
    recorder._mic_active_device_name = JABRA
    recorder._mic_monitor.stream_opened(time.monotonic() - silent_for)
    recorder._mic_monitor.observe(_analyze_pcm16(SILENT), time.monotonic())
    return recorder, switches


def test_a_silent_headset_hands_over_to_another_headset_that_is_live(tmp_path):
    host = _mic_host(_PolledStream(), sony_stream=_PolledStream([QUIET] * 50))
    recorder, switches = _on_headset(tmp_path, host, _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS + 1)

    recorder._auto_select_microphone(time.monotonic())

    assert [(kind, name) for kind, name, _ in switches] == [("mic", SONY)]


def test_the_microphone_falls_back_to_the_laptop_when_no_headset_is_live(tmp_path):
    host = _mic_host(_PolledStream(), sony_stream=_PolledStream([SILENT] * 50))
    recorder, switches = _on_headset(tmp_path, host, _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS + 1)

    recorder._auto_select_microphone(time.monotonic())

    assert [(kind, name) for kind, name, _ in switches] == [("mic", LAPTOP)]
    assert "no headset is picking anything up" in switches[0][2]["reason"]


def test_a_headset_that_is_only_briefly_quiet_is_not_second_guessed(tmp_path):
    host = _mic_host(_PolledStream(), sony_stream=_PolledStream([LOUD] * 50))
    recorder, switches = _on_headset(tmp_path, host, 2.0)

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []


def test_at_the_start_a_switched_off_headset_is_left_straight_away_for_a_live_one(tmp_path):
    host = _mic_host(_PolledStream([SILENT] * 50), sony_stream=_PolledStream([QUIET] * 50))
    recorder, switches = _on_headset(tmp_path, host, 0.0)

    recorder._auto_select_microphone(time.monotonic(), starting=True)

    assert [(kind, name) for kind, name, _ in switches] == [("mic", SONY)]


def test_at_the_start_a_live_headset_is_kept(tmp_path):
    host = _mic_host(_PolledStream([QUIET] * 50), sony_stream=_PolledStream([LOUD] * 50))
    recorder, switches = _on_headset(tmp_path, host, 0.0)

    recorder._auto_select_microphone(time.monotonic(), starting=True)

    assert switches == []


def test_from_the_laptop_every_headset_is_checked(tmp_path):
    host = _mic_host(_PolledStream([SILENT] * 50), sony_stream=_PolledStream([QUIET] * 50))
    recorder, switches = _auto_recorder(tmp_path, host, auto_microphone=True, mic_device_name=LAPTOP)
    recorder._mic_active_device_name = LAPTOP
    recorder._mic_monitor.stream_opened(time.monotonic())

    recorder._auto_select_microphone(time.monotonic())

    assert [(kind, name) for kind, name, _ in switches] == [("mic", SONY)]


def test_from_the_laptop_headsets_are_rechecked_only_every_so_often(tmp_path):
    host = _mic_host(_PolledStream([SILENT] * 500))
    recorder, switches = _auto_recorder(tmp_path, host, auto_microphone=True, mic_device_name=LAPTOP)
    recorder._mic_active_device_name = LAPTOP
    recorder._mic_monitor.stream_opened(time.monotonic())
    now = time.monotonic()

    recorder._auto_select_microphone(now)
    assert recorder._next_headset_check == now + _HEADSET_RECHECK_SECONDS

    host.streams[1] = _PolledStream([QUIET] * 50)  # unmuted, but not due to be checked yet
    recorder._auto_select_microphone(now + 1)
    assert switches == []

    recorder._auto_select_microphone(now + _HEADSET_RECHECK_SECONDS)
    assert [(kind, name) for kind, name, _ in switches] == [("mic", JABRA)]


def test_picking_a_device_by_hand_turns_its_automation_off(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, auto_microphone=True, auto_system_device=True)
    monkeypatch.setattr(Recorder, "_switch_stream", lambda self, **kwargs: None)

    recorder.switch_mic_device("Microphone Array (Realtek)")

    assert recorder._auto_mic is False
    assert recorder._auto_system is True
    assert any("Automatic microphone selection is off" in n for n in recorder.capture_notices())

    recorder.switch_system_device("Speakers", automatic=True)
    assert recorder._auto_system is True


def test_the_first_microphone_check_runs_straight_away_and_listens_to_every_headset(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, auto_microphone=True)
    calls = []

    def check(self, now, starting=False):
        calls.append(starting)
        self._watch_stop_event.set()

    monkeypatch.setattr(Recorder, "_auto_select_microphone", check)
    thread = threading.Thread(target=recorder._auto_select_devices)
    thread.start()
    thread.join(timeout=2)

    assert calls == [True]


# --- following the devices Teams is using -----------------------------------------------------------------

from meeting_scribe.audio.call_devices import CallAudioDevices  # noqa: E402


def test_the_microphone_follows_the_one_teams_is_using_over_any_headset_guess(tmp_path):
    host = _mic_host(_PolledStream([QUIET] * 50))
    recorder, switches = _on_headset(tmp_path, host, 0.0)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(LAPTOP,))

    recorder._auto_select_microphone(time.monotonic(), starting=True)

    assert switches == [("mic", LAPTOP, {"reason": "Teams is using this microphone", "automatic": True})]


def test_the_microphone_teams_is_using_is_kept_without_listening_to_anything(tmp_path):
    jabra = _PolledStream([SILENT] * 50)  # would count as dead if the headset checks ran
    host = _mic_host(jabra)
    recorder, switches = _on_headset(tmp_path, host, _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS + 1)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(JABRA,))

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []
    assert len(jabra._chunks) == 50  # never probed


def test_system_audio_follows_the_output_teams_is_playing_through_straight_away(tmp_path):
    host = _ProbeHost(
        {5: _PolledStream([LOUD] * 50), 6: _PolledStream()},
        loopbacks=[_info("Speakers [Loopback]", 5, True), _info("Headphones (Jabra) [Loopback]", 6, True)],
    )
    recorder, switches = _auto_recorder(tmp_path, host, auto_system_device=True)
    recorder._call_devices = lambda: CallAudioDevices(speakers=("Headphones (Jabra)",))
    recorder._system_active_device_name = "Speakers [Loopback]"
    recorder._system_monitor.stream_opened(time.monotonic())
    recorder._system_monitor.observe(_analyze_pcm16(LOUD), time.monotonic())  # not quiet at all

    recorder._auto_select_system_device(time.monotonic())

    assert switches == [(
        "system", "Headphones (Jabra) [Loopback]",
        {"reason": "Teams is playing the call through it", "automatic": True},
    )]


def test_teams_devices_are_ignored_once_the_automation_is_off(tmp_path):
    host = _mic_host(_PolledStream([QUIET] * 50))
    recorder, switches = _on_headset(tmp_path, host, 0.0)
    recorder._auto_mic = False
    recorder._call_devices = lambda: CallAudioDevices(microphones=(LAPTOP,))

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []


def test_teams_devices_are_asked_for_once_per_pass_not_once_per_stream(tmp_path):
    recorder = Recorder(tmp_path)
    calls = []
    recorder._call_devices = lambda: calls.append(1) or None
    recorder._call_app_devices(100.0)
    recorder._call_app_devices(100.5)
    recorder._call_app_devices(102.0)
    assert len(calls) == 2


# --- keeping system audio on the meeting's clock ---------------------------------------------------------


def _loopback_thread(tmp_path, stream):
    recorder = Recorder(tmp_path)
    recorder._pyaudio = _ProbeHost({4: stream})
    writer = _SegmentedWavWriter(tmp_path / "system.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    thread = recorder._spawn_capture_thread(
        PA, _info("Speakers [Loopback]", 4, loopback=True), writer, "system_level",
        recorder._system_monitor, "System audio", recorder._system_stop_event,
    )
    return recorder, thread


def _frames(path):
    with contextlib.closing(wave.open(str(path), "rb")) as wav:
        return wav.readframes(wav.getnframes())


def test_silence_is_written_while_nothing_plays_so_the_file_keeps_time(tmp_path):
    # The field case: WASAPI hands over nothing at all while nothing plays, so the system-audio file
    # held only the moments something played — shorter than the meeting, and out of step with the mic.
    recorder, thread = _loopback_thread(tmp_path, _PolledStream())
    time.sleep(0.8)
    recorder._system_stop_event.set()
    thread.join(timeout=2)

    frames = _frames(tmp_path / "system.wav")
    seconds = len(frames) / SAMPLE_WIDTH_BYTES / 16000
    assert 0.5 <= seconds <= 1.0
    assert frames == bytes(len(frames))


def test_audio_that_plays_is_kept_and_the_quiet_after_it_is_filled_in(tmp_path):
    recorder, thread = _loopback_thread(tmp_path, _PolledStream([LOUD]))
    time.sleep(0.8)
    recorder._system_stop_event.set()
    thread.join(timeout=2)

    frames = _frames(tmp_path / "system.wav")
    assert frames.startswith(LOUD)
    assert len(frames) / SAMPLE_WIDTH_BYTES / 16000 >= 0.5
    assert frames[len(LOUD):] == bytes(len(frames) - len(LOUD))


# --- a stream that dies is reopened ----------------------------------------------------------------------


class _FailingStream(_PolledStream):
    def get_read_available(self):
        if not self._chunks:
            raise OSError("Unanticipated host error")  # the device went away under it
        return super().get_read_available()


def test_a_capture_stream_that_dies_is_flagged_for_reopening(tmp_path):
    recorder, thread = _loopback_thread(tmp_path, _FailingStream([LOUD]))
    thread.join(timeout=2)

    assert recorder._stream_failed.is_set()
    assert any("capture stopped early" in notice for notice in recorder.capture_notices())


def test_a_stream_stopped_on_purpose_is_not_reopened(tmp_path):
    recorder, thread = _loopback_thread(tmp_path, _PolledStream())
    recorder._system_stop_event.set()
    thread.join(timeout=2)
    assert not recorder._stream_failed.is_set()


def test_reopening_backs_off_when_it_keeps_failing(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path)
    reloads = []

    def reload(self, *, reason):
        reloads.append(reason)
        self._stream_failed.set()  # the reopened stream failed again

    monkeypatch.setattr(Recorder, "reload_devices", reload)
    recorder._stream_failed.set()

    recorder._recover_failed_streams(100.0)
    recorder._recover_failed_streams(100.5)  # too soon
    recorder._recover_failed_streams(101.0)
    recorder._recover_failed_streams(102.0)  # too soon: now waiting 2s
    recorder._recover_failed_streams(103.0)

    assert len(reloads) == 3
    assert recorder._next_recovery_at == 103.0 + 4.0


def test_a_successful_reopen_says_so(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path)
    monkeypatch.setattr(Recorder, "reload_devices", lambda self, *, reason: None)
    recorder._stream_failed.set()

    recorder._recover_failed_streams(100.0)

    assert not recorder._stream_failed.is_set()
    assert any("reopened" in notice for notice in recorder.capture_notices())


def test_when_teams_has_two_microphones_open_the_one_being_recorded_is_kept(tmp_path):
    host = _mic_host(_PolledStream([QUIET] * 50))
    recorder, switches = _on_headset(tmp_path, host, 0.0)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(LAPTOP, JABRA))

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []


def test_a_computer_that_slept_mid_meeting_does_not_write_the_sleep_out_as_silence(tmp_path, monkeypatch):
    real_monotonic = time.monotonic
    started = real_monotonic()
    # Jump an hour ahead a moment after the stream opens, as a lid closed and reopened would.
    monkeypatch.setattr(
        "meeting_scribe.audio.recorder.time.monotonic",
        lambda: real_monotonic() + (3600.0 if real_monotonic() - started > 0.3 else 0.0),
    )
    recorder, thread = _loopback_thread(tmp_path, _PolledStream())
    time.sleep(0.9)
    recorder._system_stop_event.set()
    thread.join(timeout=2)

    assert len(_frames(tmp_path / "system.wav")) / SAMPLE_WIDTH_BYTES / 16000 < 2.0
