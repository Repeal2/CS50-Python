"""Automatic device selection: the microphone and system audio follow whichever devices Teams has open,
and nothing else (see audio.recorder and audio.call_devices)."""

import contextlib
import struct
import sys
import threading
import time
import wave
from types import SimpleNamespace

from meeting_scribe.audio.call_devices import CallAudioDevices
from meeting_scribe.audio.recorder import (
    SAMPLE_WIDTH_BYTES,
    Recorder,
    _SegmentedWavWriter,
    _StreamActivityMonitor,
    _analyze_pcm16,
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


# --- a stand-in PortAudio --------------------------------------------------------------------------------


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


class _Host:
    def __init__(self, streams=None, inputs=(), loopbacks=(), default_input=None):
        self.streams = streams or {}  # device index -> stream
        self.inputs = list(inputs)
        self.loopbacks = list(loopbacks)
        self.default_input = default_input

    def open(self, **kwargs):
        return self.streams[kwargs["input_device_index"]]

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


def test_a_loopback_capture_thread_can_stop_while_nothing_is_playing(tmp_path):
    # The reason loopback streams are polled: a blocking read waits for as long as the output device
    # is silent, so the thread could never be stopped — which a switch away from it depends on.
    recorder = Recorder(tmp_path)
    recorder._pyaudio = _Host({4: _PolledStream()})
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
    recorder._pyaudio = _Host({4: stream})
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


# --- following the devices Teams is using -----------------------------------------------------------------


LAPTOP, JABRA = "Microphone Array (Realtek)", "Headset Microphone (Jabra)"


def _mic_host():
    devices = [_info(LAPTOP, 0), _info(JABRA, 1)]
    return _Host(inputs=devices, default_input=devices[0])


def _following_recorder(tmp_path, host, *, mic=LAPTOP, **kwargs):
    recorder = Recorder(tmp_path, follow_call_app=True, **kwargs)
    recorder._started_at = 0.0
    recorder._pyaudio = host
    recorder._pyaudio_module = PA
    recorder._mic_active_device_name = mic
    recorder._call_devices = lambda: None  # not in a Teams call, whatever the machine running this is doing
    switches = []
    recorder.switch_system_device = lambda name, **kw: switches.append(("system", name, kw))
    recorder.switch_mic_device = lambda name, **kw: switches.append(("mic", name, kw))
    return recorder, switches


def test_the_microphone_follows_the_one_teams_is_using(tmp_path):
    recorder, switches = _following_recorder(tmp_path, _mic_host())
    recorder._call_devices = lambda: CallAudioDevices(microphones=(JABRA,))

    recorder._auto_select_microphone(time.monotonic())

    assert switches == [("mic", JABRA, {"reason": "Teams is using this microphone", "automatic": True})]


def test_the_microphone_teams_is_using_is_kept(tmp_path):
    recorder, switches = _following_recorder(tmp_path, _mic_host(), mic=JABRA)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(JABRA,))

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []


def test_nothing_changes_while_teams_is_not_using_a_microphone(tmp_path):
    # No guessing from headset names or levels: a headset is connected, but Teams hasn't opened it.
    recorder, switches = _following_recorder(tmp_path, _mic_host())
    recorder._call_devices = lambda: CallAudioDevices(speakers=("Speakers",))
    recorder._auto_select_microphone(time.monotonic())

    recorder._call_devices = lambda: None
    recorder._call_devices_cache = None
    recorder._auto_select_microphone(time.monotonic() + 60)

    assert switches == []


def test_system_audio_follows_the_output_teams_is_playing_through(tmp_path):
    host = _Host(
        loopbacks=[_info("Speakers [Loopback]", 5, True), _info("Headphones (Jabra) [Loopback]", 6, True)],
    )
    recorder, switches = _following_recorder(tmp_path, host)
    recorder._call_devices = lambda: CallAudioDevices(speakers=("Headphones (Jabra)",))
    recorder._system_active_device_name = "Speakers [Loopback]"

    recorder._auto_select_system_device(time.monotonic())

    assert switches == [(
        "system", "Headphones (Jabra) [Loopback]",
        {"reason": "Teams is playing the call through it", "automatic": True},
    )]


def test_system_audio_is_left_alone_while_teams_is_not_playing_anything(tmp_path):
    host = _Host(
        loopbacks=[_info("Speakers [Loopback]", 5, True), _info("Headphones (Jabra) [Loopback]", 6, True)],
    )
    recorder, switches = _following_recorder(tmp_path, host)
    recorder._system_active_device_name = "Speakers [Loopback]"

    recorder._auto_select_system_device(time.monotonic())

    assert switches == []


def test_when_teams_has_two_microphones_open_windows_default_for_calls_decides(tmp_path):
    recorder, switches = _following_recorder(tmp_path, _mic_host(), mic=JABRA)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(JABRA, LAPTOP), default_microphone=LAPTOP)

    recorder._auto_select_microphone(time.monotonic())

    assert [(kind, name) for kind, name, _ in switches] == [("mic", LAPTOP)]


def test_when_teams_has_two_microphones_open_the_one_being_recorded_is_kept(tmp_path):
    recorder, switches = _following_recorder(tmp_path, _mic_host(), mic=JABRA)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(LAPTOP, JABRA))

    recorder._auto_select_microphone(time.monotonic())

    assert switches == []


def test_teams_devices_are_ignored_once_following_is_off(tmp_path):
    recorder, switches = _following_recorder(tmp_path, _mic_host())
    recorder._auto_mic = False
    recorder._call_devices = lambda: CallAudioDevices(microphones=(JABRA,))

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


def _start(recorder, host, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", SimpleNamespace(PyAudio=lambda: host, paInt16=8))
    opened = []
    monkeypatch.setattr(
        Recorder, "_resolve_loopback_device", lambda self, module, name: _info(name or "Speakers [Loopback]", 9, True)
    )
    monkeypatch.setattr(Recorder, "_spawn_capture_thread", lambda self, module, info, *args: opened.append(info["name"]))
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)  # no background checks in this test
    recorder.start()
    return opened


def test_recording_starts_on_the_devices_teams_is_using(tmp_path, monkeypatch):
    host = _mic_host()
    host.loopbacks = [_info("Headphones (Jabra) [Loopback]", 6, True)]
    recorder = Recorder(tmp_path, mic_device_name=LAPTOP, follow_call_app=True)
    recorder._call_devices = lambda: CallAudioDevices(microphones=(JABRA,), speakers=("Headphones (Jabra)",))

    opened = _start(recorder, host, monkeypatch)

    assert opened == [JABRA, "Headphones (Jabra) [Loopback]"]


def test_recording_starts_on_the_settings_devices_when_teams_is_not_in_a_call(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, mic_device_name=LAPTOP, follow_call_app=True)
    recorder._call_devices = lambda: None

    opened = _start(recorder, _mic_host(), monkeypatch)

    assert opened == [LAPTOP, "Speakers [Loopback]"]
    assert recorder.capture_notices() == ()


def test_picking_a_device_by_hand_turns_following_off_for_that_stream(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, follow_call_app=True)
    monkeypatch.setattr(Recorder, "_switch_stream", lambda self, **kwargs: None)

    recorder.switch_mic_device("Microphone Array (Realtek)")

    assert recorder._auto_mic is False
    assert recorder._auto_system is True
    assert any("Automatic microphone selection is off" in n for n in recorder.capture_notices())

    recorder.switch_system_device("Speakers", automatic=True)
    assert recorder._auto_system is True


def test_the_following_thread_checks_both_streams_each_pass(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, follow_call_app=True)
    calls = []
    monkeypatch.setattr("meeting_scribe.audio.recorder._AUTO_DEVICE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(Recorder, "_auto_select_system_device", lambda self, now: calls.append("system"))

    def mic(self, now):
        calls.append("mic")
        self._watch_stop_event.set()

    monkeypatch.setattr(Recorder, "_auto_select_microphone", mic)
    thread = threading.Thread(target=recorder._auto_select_devices)
    thread.start()
    thread.join(timeout=2)

    assert calls == ["system", "mic"]


# --- keeping system audio on the meeting's clock ---------------------------------------------------------


def _loopback_thread(tmp_path, stream):
    recorder = Recorder(tmp_path)
    recorder._pyaudio = _Host({4: stream})
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
