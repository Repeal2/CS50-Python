import contextlib
import struct
import sys
import wave
from types import SimpleNamespace

import pytest

from meeting_scribe.audio.recorder import (
    MAX_WAV_DATA_BYTES,
    SAMPLE_WIDTH_BYTES,
    InputCheck,
    Recorder,
    StreamHealth,
    _ACTIVITY_DBFS,
    _analyze_pcm16,
    _pcm16_level,
    _SegmentedWavWriter,
    _SILENT_DBFS,
    _StreamActivityMonitor,
    check_input_device,
    describe_input_problems,
)


def test_pcm16_level_silence_is_zero():
    silence = struct.pack("<h", 0) * 100
    assert _pcm16_level(silence) == 0.0


def test_pcm16_level_full_scale_is_near_the_top_of_the_range():
    full_scale = struct.pack("<h", 32767) * 100
    assert _pcm16_level(full_scale) > 0.9


def test_pcm16_level_empty_chunk_is_zero():
    assert _pcm16_level(b"") == 0.0


def test_pcm16_level_never_exceeds_one():
    # Two int16 min values back-to-back forms a valid 4-byte chunk without needing struct packing.
    loudest_possible = struct.pack("<hh", -32768, -32768) * 50
    assert _pcm16_level(loudest_possible) <= 1.0


def test_pcm16_level_uses_a_log_scale_so_quiet_speech_is_still_visible():
    # A normal-volume mic might only swing +-1000 out of a possible +-32768 (~3% of full scale) — on a
    # plain linear ratio that's an all-but-invisible sliver of a meter. The dBFS scale this now uses
    # should show meaningfully more than the raw linear ratio for exactly that kind of quiet-but-real
    # signal, which is the actual bug report this was fixed for.
    quiet_speech = struct.pack("<h", 1000) * 100
    linear_ratio = 1000 / 32768
    assert _pcm16_level(quiet_speech) > linear_ratio * 3


def test_pcm16_level_silence_and_full_scale_bound_the_log_scale():
    silence = struct.pack("<h", 0) * 100
    full_scale = struct.pack("<h", 32767) * 100
    assert _pcm16_level(silence) < _pcm16_level(quiet := struct.pack("<h", 500) * 100)
    assert _pcm16_level(quiet) < _pcm16_level(full_scale)


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_recorder_start_requires_windows(tmp_path):
    recorder = Recorder(tmp_path)
    with pytest.raises(RuntimeError):
        recorder.start()


def test_recorder_defaults_to_no_device_override(tmp_path):
    recorder = Recorder(tmp_path)
    assert recorder._mic_device_name is None
    assert recorder._system_device_name is None
    assert recorder.mic_level == 0.0
    assert recorder.system_level == 0.0


def test_recorder_stores_requested_device_names(tmp_path):
    recorder = Recorder(tmp_path, mic_device_name="USB Mic", system_device_name="Speakers (Realtek)")
    assert recorder._mic_device_name == "USB Mic"
    assert recorder._system_device_name == "Speakers (Realtek)"


def _wav_frames(path) -> bytes:
    with contextlib.closing(wave.open(str(path), "rb")) as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == SAMPLE_WIDTH_BYTES
        assert wav_file.getframerate() == 16000
        return wav_file.readframes(wav_file.getnframes())


def test_max_wav_data_bytes_stays_within_what_a_riff_header_can_describe():
    # The bug this guards: RIFF sizes are 32-bit, so `wave` raises struct.error while patching the
    # header the moment a file crosses ~4 GiB. Stay under 2 GiB — plenty of readers treat those sizes
    # as signed — with room to spare for the 44-byte header itself.
    assert 44 + MAX_WAV_DATA_BYTES < 2**31


def test_short_recording_stays_in_one_plainly_named_file(tmp_path):
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    writer.write(b"\x01\x00" * 100)
    writer.close()

    assert writer.paths == (tmp_path / "mic.wav",)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mic.wav"]


def test_writer_rolls_over_to_a_new_part_instead_of_overflowing_the_header(tmp_path):
    # Stands in for a meeting long enough to fill a WAV: with a 400-byte cap, 6 x 100 bytes has to
    # spill into further parts rather than growing one file past what its header can describe.
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000, max_data_bytes=400)
    chunks = [bytes([index]) * 100 for index in range(1, 7)]
    for chunk in chunks:
        writer.write(chunk)
    writer.close()

    assert [path.name for path in writer.paths] == ["mic.wav", "mic.part2.wav"]
    # Every part is a valid, fully-finalized WAV, and no audio was dropped at the seam.
    assert b"".join(_wav_frames(path) for path in writer.paths) == b"".join(chunks)
    assert len(_wav_frames(writer.paths[0])) == 400


def test_writer_keeps_numbering_parts_past_the_second(tmp_path):
    writer = _SegmentedWavWriter(tmp_path / "system.wav", 1, SAMPLE_WIDTH_BYTES, 16000, max_data_bytes=100)
    for _ in range(3):
        writer.write(b"\x02\x00" * 50)
    writer.close()

    assert [path.name for path in writer.paths] == [
        "system.wav",
        "system.part2.wav",
        "system.part3.wav",
    ]


def test_a_chunk_larger_than_the_cap_gets_its_own_part_rather_than_looping(tmp_path):
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000, max_data_bytes=10)
    writer.write(b"\x03\x00" * 50)
    writer.write(b"\x04\x00" * 50)
    writer.close()

    assert len(writer.paths) == 2
    assert len(_wav_frames(writer.paths[0])) == 100


def test_empty_writes_do_not_start_a_new_part(tmp_path):
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000, max_data_bytes=10)
    writer.write(b"")
    writer.close()

    assert writer.paths == (tmp_path / "mic.wav",)
    assert _wav_frames(tmp_path / "mic.wav") == b""


def test_capture_notices_start_empty(tmp_path):
    assert Recorder(tmp_path).capture_notices() == ()


def test_a_dead_capture_thread_is_reported_rather_than_silently_losing_the_track(tmp_path):
    # The failure mode this exists for: a capture thread dying mid-meeting (a full WAV header, an
    # unplugged device, a driver error) used to leave the rest of that track silent with nothing
    # anywhere saying why — the meeting just came back missing one side of the conversation.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._record_capture_failure("Microphone", OSError("device disconnected"))

    assert recorder.capture_notices() == ("Microphone capture stopped early: OSError: device disconnected",)
    assert recorder.stop().notices == recorder.capture_notices()


def test_stop_reports_every_part_that_was_recorded(tmp_path):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._mic_writer = _SegmentedWavWriter(
        tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000, max_data_bytes=100
    )
    recorder._mic_writer.write(b"\x05\x00" * 50)
    recorder._mic_writer.write(b"\x05\x00" * 50)
    recorder._mic_writer.close()

    recorded = recorder.stop()

    assert [path.name for path in recorded.mic_paths] == ["mic.wav", "mic.part2.wav"]
    # No capture thread ever ran for system audio here, so it falls back to the plain single path.
    assert recorded.system_paths == (tmp_path / "system.wav",)


class _FakeStream:
    """Stands in for a PyAudio input stream: hands out `chunks`, then either raises (a device dying
    mid-meeting) or stops the recorder."""

    def __init__(self, chunks, fail_with=None, stop_event=None):
        self._chunks = list(chunks)
        self._fail_with = fail_with
        self._stop_event = stop_event
        self.stopped = False
        self.closed = False

    def read(self, frames, exception_on_overflow=False):
        if self._chunks:
            return self._chunks.pop(0)
        if self._fail_with is not None:
            raise self._fail_with
        self._stop_event.set()
        return b""

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakePyAudio:
    def __init__(self, stream):
        self._stream = stream

    def open(self, **kwargs):
        return self._stream


def _run_capture_thread(recorder, stream, path):
    recorder._pyaudio = _FakePyAudio(stream)
    device_info = {"maxInputChannels": 1, "defaultSampleRate": 16000, "index": 3}
    writer, thread = recorder._spawn_capture_thread(
        SimpleNamespace(paInt16=8), device_info, path, "mic_level", recorder._mic_monitor, "Microphone"
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    return writer


def test_capture_thread_writes_what_it_read_and_tears_the_stream_down(tmp_path):
    recorder = Recorder(tmp_path)
    stream = _FakeStream([b"\x01\x00" * 10, b"\x02\x00" * 10], stop_event=recorder._stop_event)

    writer = _run_capture_thread(recorder, stream, tmp_path / "mic.wav")

    assert _wav_frames(writer.paths[0]) == b"\x01\x00" * 10 + b"\x02\x00" * 10
    assert (stream.stopped, stream.closed) == (True, True)
    assert recorder.capture_notices() == ()
    assert recorder.mic_level == 0.0


def test_a_failing_capture_thread_still_leaves_a_readable_file_and_says_what_happened(tmp_path):
    # The original bug: a write that blew past the WAV header's 4 GiB ceiling killed this thread with a
    # struct.error, so the file was never finalized and nothing anywhere reported it. Whatever the
    # cause, the recording so far has to survive and the failure has to be visible.
    recorder = Recorder(tmp_path)
    stream = _FakeStream([b"\x07\x00" * 10], fail_with=OSError("device disconnected"))

    writer = _run_capture_thread(recorder, stream, tmp_path / "mic.wav")

    assert _wav_frames(writer.paths[0]) == b"\x07\x00" * 10
    assert recorder.capture_notices() == (
        "Microphone capture stopped early: OSError: device disconnected",
    )
    assert (stream.stopped, stream.closed) == (True, True)


def test_a_stream_that_never_opens_is_reported_without_taking_the_meeting_down(tmp_path):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._pyaudio = SimpleNamespace(
        open=lambda **kwargs: (_ for _ in ()).throw(OSError("no such device"))
    )
    device_info = {"maxInputChannels": 1, "defaultSampleRate": 16000, "index": 3}
    _writer, thread = recorder._spawn_capture_thread(
        SimpleNamespace(paInt16=8),
        device_info,
        tmp_path / "mic.wav",
        "mic_level",
        recorder._mic_monitor,
        "Microphone",
    )
    thread.join(timeout=5)

    assert recorder.capture_notices() == ("Microphone capture stopped early: OSError: no such device",)
    # The empty WAV is still valid and still finalized, so transcription reads it as silence.
    assert _wav_frames(tmp_path / "mic.wav") == b""


def _pcm16(*values, repeat=1):
    return struct.pack(f"<{len(values)}h", *values) * repeat


def _health(**overrides) -> StreamHealth:
    """A healthy stream by default; override just the field a test is about."""
    base = dict(
        seconds_captured=120.0,
        peak_dbfs=-24.0,
        heard_activity=True,
        seconds_since_activity=0.5,
        digital_silence=False,
        clipping=False,
    )
    base.update(overrides)
    return StreamHealth(**base)


# --- chunk analysis --------------------------------------------------------------------------------


def test_analyze_reports_digital_silence_for_an_all_zero_chunk():
    # The distinction the whole "wrong input" check rests on: a working microphone in a silent room
    # still has a noise floor, so exact zeros mean the device is producing nothing at all.
    stats = _analyze_pcm16(_pcm16(0, repeat=100))
    assert stats.all_zero
    assert stats.level == 0.0
    assert stats.rms_dbfs == _SILENT_DBFS


def test_analyze_does_not_call_a_quiet_noise_floor_digital_silence():
    stats = _analyze_pcm16(_pcm16(3, -2, 1, -3, repeat=25))
    assert not stats.all_zero
    assert not stats.clipped
    assert stats.rms_dbfs < _ACTIVITY_DBFS  # audible as hiss, nowhere near speech


def test_analyze_flags_a_chunk_pinned_to_the_rail_as_clipped():
    assert _analyze_pcm16(_pcm16(32767, -32768, repeat=50)).clipped


def test_analyze_handles_int16_minimum_without_overflowing():
    # abs(-32768) doesn't fit in an int16; getting this wrong reads as digital silence or a negative
    # magnitude rather than the loudest possible sample.
    stats = _analyze_pcm16(_pcm16(-32768, repeat=100))
    assert not stats.all_zero
    assert stats.clipped
    assert stats.rms_dbfs > -1.0


def test_analyze_treats_an_empty_chunk_as_nothing_at_all():
    stats = _analyze_pcm16(b"")
    assert (stats.level, stats.rms_dbfs, stats.all_zero, stats.clipped) == (
        0.0,
        _SILENT_DBFS,
        True,
        False,
    )


def test_speech_sits_above_the_activity_threshold_and_a_quiet_room_below_it():
    # Guards the threshold itself against drifting into uselessness in either direction.
    speech = _analyze_pcm16(_pcm16(3000, -3000, repeat=50))  # ~-20 dBFS
    room = _analyze_pcm16(_pcm16(30, -30, repeat=50))  # ~-60 dBFS
    assert speech.rms_dbfs >= _ACTIVITY_DBFS
    assert room.rms_dbfs < _ACTIVITY_DBFS


# --- the activity monitor --------------------------------------------------------------------------


def test_a_monitor_that_has_seen_nothing_concludes_nothing():
    health = _StreamActivityMonitor().health(now=10.0)
    assert health == StreamHealth.idle()
    assert not health.digital_silence  # "not started" is not "silent"


def test_monitor_tracks_digital_silence_across_a_whole_stream():
    monitor = _StreamActivityMonitor()
    silence = _analyze_pcm16(_pcm16(0, repeat=100))
    for tick in range(20):
        monitor.observe(silence, now=float(tick))

    health = monitor.health(now=20.0)
    assert health.digital_silence
    assert not health.heard_activity
    assert health.seconds_since_activity is None
    assert health.seconds_captured == 20.0


def test_one_non_zero_chunk_is_enough_to_rule_out_digital_silence():
    monitor = _StreamActivityMonitor()
    monitor.observe(_analyze_pcm16(_pcm16(0, repeat=100)), now=0.0)
    monitor.observe(_analyze_pcm16(_pcm16(4, -4, repeat=50)), now=1.0)
    monitor.observe(_analyze_pcm16(_pcm16(0, repeat=100)), now=2.0)

    health = monitor.health(now=2.0)
    assert not health.digital_silence
    assert not health.heard_activity  # a noise floor still isn't the room being picked up


def test_monitor_remembers_that_it_heard_something_and_how_long_ago():
    monitor = _StreamActivityMonitor()
    monitor.observe(_analyze_pcm16(_pcm16(5000, -5000, repeat=50)), now=10.0)
    monitor.observe(_analyze_pcm16(_pcm16(0, repeat=100)), now=11.0)

    health = monitor.health(now=40.0)
    assert health.heard_activity
    assert health.seconds_since_activity == 30.0
    assert health.peak_dbfs > _ACTIVITY_DBFS


def test_the_loudest_moment_of_a_stream_is_remembered():
    # What the warnings cite as evidence: "the best this device managed all meeting was -52 dBFS".
    monitor = _StreamActivityMonitor()
    monitor.observe(_analyze_pcm16(_pcm16(20000, -20000, repeat=50)), now=0.0)
    monitor.observe(_analyze_pcm16(_pcm16(0, repeat=100)), now=1.0)

    assert monitor.health(now=1.0).peak_dbfs > -6.0


def test_clipping_needs_more_than_one_hot_chunk():
    monitor = _StreamActivityMonitor()
    clipped = _analyze_pcm16(_pcm16(32767, -32768, repeat=50))

    monitor.observe(clipped, now=0.0)
    assert not monitor.health(now=0.0).clipping  # a single door slam isn't a level problem

    monitor.observe(clipped, now=0.1)
    monitor.observe(clipped, now=0.2)
    assert monitor.health(now=0.2).clipping
    # ...and it stops being news once the input settles down again.
    assert not monitor.health(now=60.0).clipping


# --- what counts as a problem ----------------------------------------------------------------------


def test_two_healthy_streams_produce_no_warnings():
    assert describe_input_problems(_health(), _health()) == ()


def test_nothing_is_concluded_before_either_stream_has_run_long_enough():
    just_started = _health(seconds_captured=2.0, digital_silence=True, heard_activity=False)
    assert describe_input_problems(just_started, _health()) == ()


def test_digital_silence_is_called_out_on_its_own_evidence():
    dead = _health(seconds_captured=30.0, digital_silence=True, heard_activity=False, peak_dbfs=-120.0)
    (problem,) = describe_input_problems(dead, _health(heard_activity=False))

    assert problem.startswith("Microphone:")
    assert "digital silence" in problem
    # Doesn't need the other stream to have heard anything — a dead device is a fact by itself.


def test_a_silent_mic_is_only_questioned_when_the_other_side_was_audible():
    quiet_mic = _health(seconds_captured=300.0, heard_activity=False, seconds_since_activity=None)

    # Nobody has said anything on either track: that's a quiet meeting, not a misconfigured one.
    assert describe_input_problems(quiet_mic, _health(heard_activity=False)) == ()

    (problem,) = describe_input_problems(quiet_mic, _health(heard_activity=True))
    assert problem.startswith("Microphone:")
    assert "check the right device is selected" in problem


def test_a_silent_mic_is_given_a_long_fuse():
    # A minute of nobody speaking at the top of a meeting is normal and must not warn.
    early = _health(seconds_captured=45.0, heard_activity=False, seconds_since_activity=None)
    assert describe_input_problems(early, _health(heard_activity=True)) == ()


def test_system_audio_is_judged_the_same_way_as_the_mic():
    quiet_system = _health(seconds_captured=300.0, heard_activity=False, seconds_since_activity=None)
    (problem,) = describe_input_problems(_health(heard_activity=True), quiet_system)
    assert problem.startswith("System audio:")


def test_both_streams_can_be_reported_at_once():
    dead = _health(seconds_captured=300.0, digital_silence=True, heard_activity=False)
    problems = describe_input_problems(dead, dead)
    assert len(problems) == 2


def test_clipping_is_reported_for_an_otherwise_healthy_stream():
    (problem,) = describe_input_problems(_health(clipping=True), _health())
    assert "clipping" in problem


def test_a_fresh_recorder_warns_about_nothing(tmp_path):
    recorder = Recorder(tmp_path)
    assert recorder.input_problems() == ()
    assert recorder.mic_health() == StreamHealth.idle()
    assert recorder.system_health() == StreamHealth.idle()


# --- the device that actually got used ---------------------------------------------------------------


def _device(name, index, channels=2):
    return {
        "name": name,
        "index": index,
        "maxInputChannels": channels,
        "defaultSampleRate": 48000,
    }


class _FakeAudioHost:
    """Enough of PyAudio's device enumeration to exercise how a device gets chosen."""

    def __init__(self, devices, default_input, loopbacks=(), default_output_index=None):
        self._devices = list(devices)
        self._default_input = default_input
        self._loopbacks = list(loopbacks)
        self._default_output_index = default_output_index

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, index):
        return self._devices[index]

    def get_default_input_device_info(self):
        return self._default_input

    def get_loopback_device_info_generator(self):
        return iter(self._loopbacks)

    def get_host_api_info_by_type(self, _host_api_type):
        return {"defaultOutputDevice": self._default_output_index}


def test_a_microphone_windows_no_longer_has_is_substituted_but_said_out_loud(tmp_path):
    # The wrong-input case that needs no signal analysis at all: the headset was left at home, the app
    # quietly records the laptop's array mic instead, and an hour later the meeting sounds like a room.
    recorder = Recorder(tmp_path, mic_device_name="Jabra Evolve")
    built_in = _device("Microphone Array (Realtek)", 0)
    recorder._pyaudio = _FakeAudioHost([built_in], default_input=built_in)

    assert recorder._resolve_input_device("Jabra Evolve") is built_in
    (notice,) = recorder.capture_notices()
    assert "Jabra Evolve" in notice and "Microphone Array (Realtek)" in notice


def test_the_selected_microphone_is_used_without_comment_when_it_is_there(tmp_path):
    recorder = Recorder(tmp_path, mic_device_name="Jabra Evolve")
    jabra = _device("Jabra Evolve", 1)
    built_in = _device("Microphone Array (Realtek)", 0)
    recorder._pyaudio = _FakeAudioHost([built_in, jabra], default_input=built_in)

    assert recorder._resolve_input_device("Jabra Evolve") is jabra
    assert recorder.capture_notices() == ()


def test_falling_back_to_the_default_is_not_worth_saying_when_nothing_was_asked_for(tmp_path):
    recorder = Recorder(tmp_path)
    built_in = _device("Microphone Array (Realtek)", 0)
    recorder._pyaudio = _FakeAudioHost([built_in], default_input=built_in)

    assert recorder._resolve_input_device(None) is built_in
    assert recorder.capture_notices() == ()


def test_a_missing_system_audio_device_is_reported_the_same_way(tmp_path):
    recorder = Recorder(tmp_path, system_device_name="Headphones (Jabra)")
    speakers = {"name": "Speakers (Realtek)", "index": 0, "isLoopbackDevice": False}
    speakers_loopback = {"name": "Speakers (Realtek) [Loopback]", "index": 5, "maxInputChannels": 2}
    recorder._pyaudio = _FakeAudioHost(
        [speakers], default_input=speakers, loopbacks=[speakers_loopback], default_output_index=0
    )

    resolved = recorder._resolve_loopback_device(SimpleNamespace(paWASAPI=13), "Headphones (Jabra)")

    assert resolved is speakers_loopback
    (notice,) = recorder.capture_notices()
    assert "Headphones (Jabra)" in notice and "Speakers (Realtek) [Loopback]" in notice


# --- what the capture thread feeds the monitor -------------------------------------------------------


def test_a_stream_of_pure_silence_is_visible_in_the_recorders_health(tmp_path):
    recorder = Recorder(tmp_path)
    stream = _FakeStream([_pcm16(0, repeat=100)] * 3, stop_event=recorder._stop_event)

    _run_capture_thread(recorder, stream, tmp_path / "mic.wav")

    health = recorder.mic_health()
    assert health.digital_silence
    assert not health.heard_activity


def test_a_stream_that_hears_the_room_is_visible_in_the_recorders_health(tmp_path):
    recorder = Recorder(tmp_path)
    stream = _FakeStream([_pcm16(6000, -6000, repeat=50)] * 3, stop_event=recorder._stop_event)

    _run_capture_thread(recorder, stream, tmp_path / "mic.wav")

    health = recorder.mic_health()
    assert health.heard_activity
    assert not health.digital_silence
    assert health.peak_dbfs > _ACTIVITY_DBFS


# --- the pre-flight microphone test ------------------------------------------------------------------


def _check(**overrides) -> InputCheck:
    base = dict(
        device_name="Jabra Evolve",
        requested_name="Jabra Evolve",
        peak_dbfs=-18.0,
        heard_activity=True,
        digital_silence=False,
        clipping=False,
    )
    base.update(overrides)
    return InputCheck(**base)


def test_a_good_microphone_test_says_so():
    result = _check()
    assert result.is_ok
    assert "sounds fine" in result.summary
    assert "-18 dBFS" in result.summary


def test_a_dead_microphone_test_names_the_likely_causes():
    result = _check(heard_activity=False, digital_silence=True, peak_dbfs=-120.0)
    assert not result.is_ok
    assert "No signal at all" in result.summary
    assert "muted at the driver" in result.summary


def test_a_quiet_microphone_test_distinguishes_itself_from_a_dead_one():
    result = _check(heard_activity=False, peak_dbfs=-52.0)
    assert not result.is_ok
    assert "nothing above speech level" in result.summary
    assert "-52 dBFS" in result.summary


def test_a_hot_microphone_test_says_to_turn_it_down():
    result = _check(clipping=True, peak_dbfs=-0.4)
    assert not result.is_ok
    assert "clipping" in result.summary


def test_a_microphone_test_reports_when_it_had_to_test_something_else():
    result = _check(requested_name="Jabra Evolve", device_name="Microphone Array (Realtek)")
    assert result.substituted_device
    assert not result.is_ok  # it works, but it isn't the device the meeting was set up to use
    assert "'Jabra Evolve' isn't available" in result.summary
    assert "Microphone Array (Realtek)" in result.summary


def test_a_test_of_the_windows_default_is_never_a_substitution():
    assert not _check(requested_name=None, device_name="Microphone Array (Realtek)").substituted_device


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_checking_an_input_device_requires_windows():
    with pytest.raises(RuntimeError):
        check_input_device("Jabra Evolve")
