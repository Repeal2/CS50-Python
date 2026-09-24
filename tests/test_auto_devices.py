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
    _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS,
    _SegmentedWavWriter,
    _SILENT_DBFS,
    _StreamActivityMonitor,
    _SYSTEM_QUIET_BEFORE_SEARCH_SECONDS,
    _analyze_pcm16,
    _probe_devices,
    choose_fallback_microphone,
    find_headset_microphone,
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


def test_the_headset_picked_in_settings_wins_over_name_matching():
    names = INPUTS + ["USB Audio Device"]
    assert find_headset_microphone(names, configured="USB Audio Device") == "USB Audio Device"


def test_a_headset_picked_in_settings_that_is_not_connected_is_not_used():
    assert find_headset_microphone(INPUTS, configured="USB Audio Device") is None


def test_without_a_setting_the_headset_is_found_by_name():
    assert find_headset_microphone(INPUTS, configured=None) == "Headset Microphone (Jabra Evol"
    assert find_headset_microphone(["Microphone Array (Realtek)"], configured=None) is None


def test_the_fallback_is_the_microphone_chosen_in_settings():
    fallback = choose_fallback_microphone(
        INPUTS, configured="Microphone Array (Realtek)", default_name=None, headset=INPUTS[3]
    )
    assert fallback == "Microphone Array (Realtek)"


def test_the_fallback_is_never_the_headset_even_when_windows_made_it_the_default():
    fallback = choose_fallback_microphone(
        INPUTS, configured=INPUTS[3], default_name=INPUTS[2], headset=INPUTS[3]
    )
    assert fallback == "Microphone Array (Realtek)"


def test_the_fallback_skips_loopback_inputs():
    fallback = choose_fallback_microphone(
        ["Stereo Mix (Realtek)", "Headset Microphone (Jabra)", "Microphone (USB)"],
        configured=None,
        default_name=None,
        headset="Headset Microphone (Jabra)",
    )
    assert fallback == "Microphone (USB)"


def test_a_live_headset_is_kept():
    assert next_microphone(
        active="Headset", headset="Headset", fallback="Laptop",
        active_seconds_without_signal=1.0, headset_is_live=None,
    ) is None


def test_a_headset_that_has_gone_digitally_silent_hands_over_to_the_laptop():
    assert next_microphone(
        active="Headset", headset="Headset", fallback="Laptop",
        active_seconds_without_signal=_HEADSET_SILENT_BEFORE_FALLBACK_SECONDS, headset_is_live=None,
    ) == "Laptop"


def test_the_headset_takes_back_over_once_it_is_live_again():
    assert next_microphone(
        active="Laptop", headset="Headset", fallback="Laptop",
        active_seconds_without_signal=0.0, headset_is_live=True,
    ) == "Headset"
    assert next_microphone(
        active="Laptop", headset="Headset", fallback="Laptop",
        active_seconds_without_signal=0.0, headset_is_live=False,
    ) is None


def test_the_same_headset_under_a_shortened_name_counts_as_the_headset():
    assert next_microphone(
        active="Headset Microphone (Jabra Evol", headset="Headset Microphone (Jabra Evolve2 65)",
        fallback="Laptop", active_seconds_without_signal=0.0, headset_is_live=True,
    ) is None


def test_nothing_changes_without_a_headset_connected():
    assert next_microphone(
        active="Laptop", headset=None, fallback="Laptop",
        active_seconds_without_signal=99.0, headset_is_live=None,
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


def _mic_host(headset_stream):
    laptop, headset = _info("Microphone Array (Realtek)", 0), _info("Headset Microphone (Jabra)", 1)
    return _ProbeHost({1: headset_stream}, inputs=[laptop, headset], default_input=headset)


def test_the_microphone_falls_back_to_the_laptop_when_the_headset_goes_silent(tmp_path):
    recorder, switches = _auto_recorder(
        tmp_path, _mic_host(_PolledStream()), auto_microphone=True,
        mic_device_name="Microphone Array (Realtek)",
    )
    recorder._mic_active_device_name = "Headset Microphone (Jabra)"
    recorder._mic_monitor.stream_opened(time.monotonic() - _HEADSET_SILENT_BEFORE_FALLBACK_SECONDS - 1)
    recorder._mic_monitor.observe(_analyze_pcm16(SILENT), time.monotonic())

    recorder._auto_select_microphone(time.monotonic())

    assert [(kind, name) for kind, name, _ in switches] == [("mic", "Microphone Array (Realtek)")]
    assert "gone silent" in switches[0][2]["reason"]


def test_the_microphone_returns_to_the_headset_once_it_picks_something_up(tmp_path):
    recorder, switches = _auto_recorder(
        tmp_path, _mic_host(_PolledStream([QUIET] * 50)), auto_microphone=True
    )
    recorder._mic_active_device_name = "Microphone Array (Realtek)"
    recorder._mic_monitor.stream_opened(time.monotonic())

    recorder._auto_select_microphone(time.monotonic())

    assert [(kind, name) for kind, name, _ in switches] == [("mic", "Headset Microphone (Jabra)")]


def test_a_muted_headset_is_not_switched_back_to(tmp_path):
    recorder, switches = _auto_recorder(
        tmp_path, _mic_host(_PolledStream([SILENT] * 50)), auto_microphone=True
    )
    recorder._mic_active_device_name = "Microphone Array (Realtek)"
    recorder._mic_monitor.stream_opened(time.monotonic())

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []


def test_picking_a_device_by_hand_turns_its_automation_off(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, auto_microphone=True, auto_system_device=True)
    monkeypatch.setattr(Recorder, "_switch_stream", lambda self, **kwargs: None)

    recorder.switch_mic_device("Microphone Array (Realtek)")

    assert recorder._auto_mic is False
    assert recorder._auto_system is True
    assert any("Automatic microphone selection is off" in n for n in recorder.capture_notices())

    recorder.switch_system_device("Speakers", automatic=True)
    assert recorder._auto_system is True
