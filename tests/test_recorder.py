import contextlib
import struct
import sys
import threading
import time
import wave
from dataclasses import replace
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
    _ChunkStats,
    _SegmentedWavWriter,
    _SILENT_DBFS,
    _StreamActivityMonitor,
    check_input_device,
    PartTiming,
    describe_input_problems,
    describe_part_timing,
    discover_wav_parts,
    load_part_offsets,
    part_offsets_path,
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


def test_discover_wav_parts_finds_a_single_unsuffixed_file(tmp_path):
    (tmp_path / "mic.wav").write_bytes(b"fake wav bytes")

    assert discover_wav_parts(tmp_path / "mic.wav") == (tmp_path / "mic.wav",)


def test_discover_wav_parts_finds_every_part_a_writer_actually_wrote(tmp_path):
    # Regression test for session.retry_meeting_transcription: given just the base path, this has to
    # reconstruct exactly what _SegmentedWavWriter would have reported as .paths while it was live —
    # since retrying happens after that writer (and the whole Recorder) is long gone.
    writer = _SegmentedWavWriter(tmp_path / "system.wav", 1, SAMPLE_WIDTH_BYTES, 16000, max_data_bytes=100)
    for _ in range(3):
        writer.write(b"\x02\x00" * 50)
    writer.close()

    assert discover_wav_parts(tmp_path / "system.wav") == writer.paths


def test_discover_wav_parts_returns_empty_when_nothing_was_ever_recorded(tmp_path):
    assert discover_wav_parts(tmp_path / "mic.wav") == ()


def test_discover_wav_parts_stops_at_the_first_gap(tmp_path):
    # Parts are always written sequentially with no gaps, so a "part3" with no "part2" beside it isn't a
    # real scenario — but stopping at the first missing number rather than scanning past it is still the
    # safer contract to document and test.
    (tmp_path / "mic.wav").write_bytes(b"first part")
    (tmp_path / "mic.part3.wav").write_bytes(b"orphaned part")

    assert discover_wav_parts(tmp_path / "mic.wav") == (tmp_path / "mic.wav",)


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


class _FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_writer_notes_when_each_part_started_on_the_meetings_clock(tmp_path):
    clock = _FakeClock()
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000, clock=clock)
    writer.write(b"\x00\x00" * 16000)  # 1 s of audio...
    clock.now = 125.0  # ...in a part that was open for 125 s (a device delivering too little)
    writer.start_new_part(1, 16000)
    writer.write(b"\x00\x00" * 8000)
    clock.now = 130.0
    writer.close()

    assert [(t.path.name, t.started_seconds, t.ended_seconds, t.audio_seconds) for t in writer.part_timings] == [
        ("mic.wav", 0.0, 125.0, 1.0),
        ("mic.part2.wav", 125.0, 130.0, 0.5),
    ]
    # Saved beside the audio, so a retried transcription places the parts the same way.
    assert part_offsets_path(tmp_path / "mic.wav") == tmp_path / "mic.parts.json"
    assert load_part_offsets(tmp_path / "mic.wav") == {tmp_path / "mic.wav": 0.0, tmp_path / "mic.part2.wav": 125.0}


def test_load_part_offsets_is_empty_for_a_meeting_recorded_before_they_were_noted(tmp_path):
    assert load_part_offsets(tmp_path / "mic.wav") == {}
    part_offsets_path(tmp_path / "mic.wav").write_text("not json", encoding="utf-8")
    assert load_part_offsets(tmp_path / "mic.wav") == {}


def test_a_part_holding_more_audio_than_time_passed_is_reported(tmp_path):
    inflated = PartTiming(tmp_path / "mic.wav", started_seconds=0.0, ended_seconds=15.0, audio_seconds=6360.0)
    normal = PartTiming(tmp_path / "mic.part2.wav", started_seconds=15.0, ended_seconds=5400.0, audio_seconds=5399.5)

    assert describe_part_timing("Microphone", normal) is None
    assert describe_part_timing("Microphone", inflated) == (
        "Microphone: mic.wav holds 106 min 00 s of audio but was only recording for 15 s — the device "
        "delivered audio faster than real time, so timestamps within that part may be off."
    )


def test_stop_returns_each_parts_start_time(tmp_path):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    clock = _FakeClock()
    recorder._mic_writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000, clock=clock)
    recorder._mic_writer.write(b"\x00\x00" * 160)
    clock.now = 42.0
    recorder._mic_writer.start_new_part(1, 16000)
    recorder._mic_writer.write(b"\x00\x00" * 160)
    recorder._mic_writer.close()

    recorded = recorder.stop()

    assert recorded.mic_offsets == {tmp_path / "mic.wav": 0.0, tmp_path / "mic.part2.wav": 42.0}
    assert recorded.system_offsets == {}


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
    writer = _SegmentedWavWriter(path, 1, SAMPLE_WIDTH_BYTES, 16000)
    thread = recorder._spawn_capture_thread(
        SimpleNamespace(paInt16=8),
        device_info,
        writer,
        "mic_level",
        recorder._mic_monitor,
        "Microphone",
        recorder._mic_stop_event,
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    return writer


def test_capture_thread_writes_what_it_read_and_tears_the_stream_down(tmp_path):
    recorder = Recorder(tmp_path)
    stream = _FakeStream([b"\x01\x00" * 10, b"\x02\x00" * 10], stop_event=recorder._mic_stop_event)

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
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    thread = recorder._spawn_capture_thread(
        SimpleNamespace(paInt16=8),
        device_info,
        writer,
        "mic_level",
        recorder._mic_monitor,
        "Microphone",
        recorder._mic_stop_event,
    )
    thread.join(timeout=5)

    assert recorder.capture_notices() == ("Microphone capture stopped early: OSError: no such device",)
    # The empty WAV is still valid and still finalized, so transcription reads it as silence.
    assert _wav_frames(tmp_path / "mic.wav") == b""


# --- live device switching --------------------------------------------------------------------------


def _finished_thread() -> threading.Thread:
    """A Thread object that has already run to completion — stands in for a capture thread's previous
    incarnation so a test can hand it to the recorder without a real (racy) capture loop in flight."""
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    return thread


def test_switch_mic_device_rolls_into_a_new_part_with_the_new_devices_parameters(tmp_path):
    # The bug this fixes: switching mics mid-meeting used to silently keep recording whatever device was
    # originally opened, with no error, no rollover, nothing — a full-length file of an idle device. A
    # switch should instead close out the old part and start a fresh one on the new device.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0

    recorder._mic_writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    recorder._mic_writer.write(b"\x01\x00" * 10)
    recorder._mic_thread = _finished_thread()

    # The new device is stereo at a different sample rate — a WAV part can't change either mid-file,
    # which is exactly why a switch has to roll into a new part rather than continuing the old one.
    new_device = {"name": "USB Headset", "index": 1, "maxInputChannels": 2, "defaultSampleRate": 48000}
    stream = _FakeStream([b"\x02\x00\x03\x00" * 5], stop_event=recorder._mic_stop_event)
    recorder._pyaudio = _FakeAudioHost([new_device], default_input=new_device)
    recorder._pyaudio.open = lambda **kwargs: stream
    recorder._pyaudio_module = SimpleNamespace(paInt16=8)

    recorder.switch_mic_device("USB Headset")
    recorder._mic_thread.join(timeout=5)

    assert [path.name for path in recorder._mic_writer.paths] == ["mic.wav", "mic.part2.wav"]
    assert recorder._mic_device_name == "USB Headset"
    assert recorder.capture_notices() == ("Microphone switched to 'USB Headset' mid-meeting.",)

    with contextlib.closing(wave.open(str(tmp_path / "mic.wav"), "rb")) as first_part:
        assert first_part.getnchannels() == 1
        assert first_part.getframerate() == 16000
        assert first_part.readframes(first_part.getnframes()) == b"\x01\x00" * 10

    with contextlib.closing(wave.open(str(tmp_path / "mic.part2.wav"), "rb")) as second_part:
        assert second_part.getnchannels() == 2
        assert second_part.getframerate() == 48000
        assert second_part.readframes(second_part.getnframes()) == b"\x02\x00\x03\x00" * 5


def test_switching_the_microphone_does_not_touch_the_system_audio_stream(tmp_path):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0

    recorder._mic_writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    recorder._mic_writer.write(b"\x01\x00" * 10)
    recorder._mic_thread = _finished_thread()

    recorder._system_writer = _SegmentedWavWriter(tmp_path / "system.wav", 2, SAMPLE_WIDTH_BYTES, 48000)
    recorder._system_writer.write(b"\x09\x00\x0a\x00" * 5)
    system_thread = _finished_thread()
    recorder._system_thread = system_thread

    new_device = {"name": "USB Headset", "index": 1, "maxInputChannels": 1, "defaultSampleRate": 16000}
    stream = _FakeStream([b"\x02\x00" * 5], stop_event=recorder._mic_stop_event)
    recorder._pyaudio = _FakeAudioHost([new_device], default_input=new_device)
    recorder._pyaudio.open = lambda **kwargs: stream
    recorder._pyaudio_module = SimpleNamespace(paInt16=8)

    recorder.switch_mic_device("USB Headset")
    recorder._mic_thread.join(timeout=5)

    # Nothing about the system-audio side was stopped, closed, or rolled over by a mic-only switch.
    assert recorder._system_thread is system_thread
    assert recorder._system_writer.paths == (tmp_path / "system.wav",)
    recorder._system_writer.close()


def test_a_recorder_that_never_started_refuses_to_switch(tmp_path):
    recorder = Recorder(tmp_path)
    with pytest.raises(RuntimeError):
        recorder.switch_mic_device("USB Headset")


def test_switch_records_a_notice_when_the_old_capture_thread_does_not_stop_in_time(tmp_path, monkeypatch):
    # A capture thread still blocked inside a native stream.read() call after the join timeout is
    # presumably reading from a hung or disconnected device — the switch still has to proceed (there's no
    # way to interrupt a blocked native read from here), but this should be visible rather than silent,
    # since the old thread may still be touching the writer the new thread is about to write to.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._mic_writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    recorder._mic_thread = _finished_thread()

    new_device = {"name": "USB Headset", "index": 1, "maxInputChannels": 1, "defaultSampleRate": 16000}
    stream = _FakeStream([b"\x02\x00" * 5], stop_event=recorder._mic_stop_event)
    recorder._pyaudio = _FakeAudioHost([new_device], default_input=new_device)
    recorder._pyaudio.open = lambda **kwargs: stream
    recorder._pyaudio_module = SimpleNamespace(paInt16=8)
    monkeypatch.setattr(Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: True))

    recorder.switch_mic_device("USB Headset")
    recorder._mic_thread.join(timeout=5)

    assert any("didn't stop within 5s" in notice for notice in recorder.capture_notices())



class _HungStream:
    """A stream whose first read() blocks until released — a hung or disconnected device's driver."""

    def __init__(self, chunk):
        self.release = threading.Event()
        self.entered_read = threading.Event()
        self._chunk = chunk
        self.closed = False

    def read(self, frames, exception_on_overflow=False):
        self.entered_read.set()
        self.release.wait(timeout=10)
        return self._chunk

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def test_a_capture_thread_that_unblocks_after_being_switched_away_from_stays_stopped(tmp_path, monkeypatch):
    # Regression test: the stop event is shared with the replacement thread and cleared for it, so an
    # old thread that was stuck in read() during the switch used to wake up, see the cleared event, and
    # carry on — writing the old device's audio into the new part and eventually closing the writer
    # out from under the new thread.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    recorder._mic_writer = writer

    hung = _HungStream(b"\x09\x00" * 5)
    recorder._pyaudio = _FakePyAudio(hung)
    old_device = {"name": "Old Mic", "index": 0, "maxInputChannels": 1, "defaultSampleRate": 16000}
    recorder._mic_thread = recorder._spawn_capture_thread(
        SimpleNamespace(paInt16=8), old_device, writer, "mic_level", recorder._mic_monitor,
        "Microphone", recorder._mic_stop_event,
    )
    assert hung.entered_read.wait(timeout=5)
    old_thread = recorder._mic_thread

    new_device = {"name": "USB Headset", "index": 1, "maxInputChannels": 1, "defaultSampleRate": 16000}
    new_stream = _FakeStream([b"\x02\x00" * 5] * 3)
    new_stream.read_gate = threading.Event()
    original_read = new_stream.read

    def gated_read(frames, exception_on_overflow=False):
        new_stream.read_gate.wait(timeout=10)
        if not new_stream._chunks:
            recorder._mic_stop_event.set()
            return b""
        return original_read(frames)

    new_stream.read = gated_read
    recorder._pyaudio = _FakeAudioHost([new_device], default_input=new_device)
    recorder._pyaudio.open = lambda **kwargs: new_stream
    recorder._pyaudio_module = SimpleNamespace(paInt16=8)
    monkeypatch.setattr(Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: thread.is_alive()))

    recorder.switch_mic_device("USB Headset")

    hung.release.set()  # the old device's read finally returns
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()

    new_stream.read_gate.set()
    recorder._mic_thread.join(timeout=5)

    # Nothing from the old device landed in the new part, and the new thread wasn't cut off early.
    with contextlib.closing(wave.open(str(tmp_path / "mic.part2.wav"), "rb")) as second_part:
        assert second_part.readframes(second_part.getnframes()) == b"\x02\x00" * 15
    assert not any("capture stopped early" in notice for notice in recorder.capture_notices())



def test_switching_system_audio_away_from_a_silent_loopback_device_does_not_leave_it_running(tmp_path, monkeypatch):
    # The system-audio case of the test above, and the likeliest way to hit it: a WASAPI loopback stream
    # delivers nothing while its output device is silent, so read() on the old device blocks as soon as
    # playback moves to the new one — the old thread is all but guaranteed to still be stuck when the
    # switch gives up waiting on it. It must stay stopped once it unblocks, and stop() must not tear
    # PortAudio down while it's still blocked.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    writer = _SegmentedWavWriter(tmp_path / "system.wav", 2, SAMPLE_WIDTH_BYTES, 48000)
    recorder._system_writer = writer

    hung = _HungStream(b"\x09\x00\x09\x00" * 5)
    recorder._pyaudio = _FakePyAudio(hung)
    old_device = {"name": "Speakers [Loopback]", "index": 5, "maxInputChannels": 2, "defaultSampleRate": 48000}
    recorder._system_thread = recorder._spawn_capture_thread(
        SimpleNamespace(paInt16=8), old_device, writer, "system_level", recorder._system_monitor,
        "System audio", recorder._system_stop_event,
    )
    assert hung.entered_read.wait(timeout=5)
    old_thread = recorder._system_thread

    new_device = {
        "name": "Headphones [Loopback]", "index": 6, "maxInputChannels": 2, "defaultSampleRate": 44100,
        "isLoopbackDevice": True,
    }
    new_stream = _FakeStream([b"\x02\x00\x03\x00" * 5] * 2, stop_event=recorder._system_stop_event)
    terminated = []
    host = _FakeAudioHost([], default_input=None, loopbacks=[new_device])
    host.open = lambda **kwargs: new_stream
    host.terminate = lambda: terminated.append(True)
    recorder._pyaudio = host
    recorder._pyaudio_module = SimpleNamespace(paInt16=8)
    monkeypatch.setattr(
        Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: thread.is_alive())
    )

    recorder.switch_system_device("Headphones [Loopback]")
    recorder._system_thread.join(timeout=5)

    # The old loopback thread is still blocked — stop() must leave PortAudio alone.
    assert old_thread.is_alive()
    recorder.stop()
    assert terminated == []
    assert any("System audio capture thread didn't stop" in n for n in recorder.capture_notices())

    # Playback resumes on the old device later: its thread must not write into the new part.
    hung.release.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()
    with contextlib.closing(wave.open(str(tmp_path / "system.part2.wav"), "rb")) as second_part:
        assert second_part.getframerate() == 44100
        assert second_part.readframes(second_part.getnframes()) == b"\x02\x00\x03\x00" * 10
    assert not any("capture stopped early" in n for n in recorder.capture_notices())


def test_stop_does_not_terminate_pyaudio_while_a_switched_away_thread_is_still_stuck(tmp_path, monkeypatch):
    # The old thread from a switch that timed out is still inside a native read on PortAudio — stop()
    # terminating PortAudio under it is the same use-after-free as for a current thread.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    stuck.start()
    recorder._orphaned_threads.append((stuck, "Microphone"))
    terminated = []
    recorder._pyaudio = SimpleNamespace(terminate=lambda: terminated.append(True))
    try:
        recorder.stop()
    finally:
        release.set()
        stuck.join(timeout=5)

    assert terminated == []


# --- stop() and a capture thread that refuses to stop -----------------------------------------------


def test_stop_does_not_terminate_pyaudio_when_a_capture_thread_is_stuck(tmp_path, monkeypatch):
    # Regression test: terminating PortAudio while a capture thread might still be blocked inside a
    # native stream.read() call on it is a use-after-free from that thread's point of view — exactly the
    # kind of thing that crashes the whole process natively, with no Python exception for anything to
    # catch. stop() must not do that just because a capture thread's 5-second join timed out.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._mic_thread = _finished_thread()  # any non-None Thread; join() itself is monkeypatched away
    terminated = []
    recorder._pyaudio = SimpleNamespace(terminate=lambda: terminated.append(True))
    monkeypatch.setattr(Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: True))

    recorder.stop()

    assert terminated == []
    assert any("didn't stop within 5s" in notice for notice in recorder.capture_notices())


def test_stop_closes_a_stuck_threads_file_so_it_is_still_a_readable_wav(tmp_path, monkeypatch):
    # Seen in the field: WASAPI loopback delivers nothing while nothing plays through the selected
    # output device, so a system-audio thread can sit in its very first read() for a whole meeting.
    # `wave` only writes a header with the first chunk, so the file was left at 0 bytes, which FFmpeg
    # rejects as "Invalid data found" — and that failed the whole meeting's transcription.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._system_thread = _finished_thread()
    recorder._system_writer = _SegmentedWavWriter(tmp_path / "system.wav", 2, SAMPLE_WIDTH_BYTES, 48000)
    recorder._pyaudio = SimpleNamespace(terminate=lambda: None)
    monkeypatch.setattr(Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: True))
    assert (tmp_path / "system.wav").stat().st_size == 0

    recorded = recorder.stop()

    assert recorded.system_paths == (tmp_path / "system.wav",)
    with contextlib.closing(wave.open(str(tmp_path / "system.wav"), "rb")) as wav_file:
        assert (wav_file.getnchannels(), wav_file.getframerate(), wav_file.getnframes()) == (2, 48000, 0)
    # If the thread ever wakes, its write is refused rather than touching a closed file.
    with pytest.raises(ValueError, match="already closed"):
        recorder._system_writer.write(b"\x00" * 4)


def test_stop_still_terminates_pyaudio_when_capture_threads_stop_in_time(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._mic_thread = _finished_thread()
    terminated = []
    recorder._pyaudio = SimpleNamespace(terminate=lambda: terminated.append(True))
    monkeypatch.setattr(Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: False))

    recorder.stop()

    assert terminated == [True]


def test_join_capture_thread_returns_false_once_the_thread_has_actually_finished(tmp_path):
    thread = _finished_thread()
    assert Recorder._join_capture_thread(thread, timeout=1.0) is False


def test_join_capture_thread_returns_true_for_a_thread_still_running_past_the_timeout(tmp_path):
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    try:
        assert Recorder._join_capture_thread(thread, timeout=0.05) is True
    finally:
        release.set()
        thread.join(timeout=5)


# --- noticing a Windows default-device change on its own --------------------------------------------


def _run_watcher_until(recorder, condition, timeout=2.0) -> None:
    """Starts the device watcher, waits (briefly) for `condition` to become true, then always stops it
    again — a real meeting's watcher would otherwise poll for the rest of the process."""
    recorder._DEVICE_POLL_SECONDS = 0.01
    recorder._watch_stop_event.clear()
    recorder._watcher_thread = threading.Thread(target=recorder._watch_devices, daemon=True)
    recorder._watcher_thread.start()
    try:
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        recorder._watch_stop_event.set()
        recorder._watcher_thread.join(timeout=5)


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
    stream = _FakeStream([_pcm16(0, repeat=100)] * 3, stop_event=recorder._mic_stop_event)

    _run_capture_thread(recorder, stream, tmp_path / "mic.wav")

    health = recorder.mic_health()
    assert health.digital_silence
    assert not health.heard_activity


def test_a_stream_that_hears_the_room_is_visible_in_the_recorders_health(tmp_path):
    recorder = Recorder(tmp_path)
    stream = _FakeStream([_pcm16(6000, -6000, repeat=50)] * 3, stop_event=recorder._mic_stop_event)

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


# --- reloading the device list mid-meeting ----------------------------------------------------------


class _ReloadableAudio:
    """A stand-in for the pyaudiowpatch module whose PyAudio() returns whatever `hosts` says the device
    list looks like *now* — PortAudio's real behavior being that only a fresh init sees changes."""

    paInt16 = 8
    paWASAPI = 13

    def __init__(self, host):
        self.host = host
        self.created = 0

    def PyAudio(self):
        self.created += 1
        return self.host


def _reload_ready_recorder(tmp_path, host, *, mic_device_name=None, system_device_name=None):
    """A Recorder mid-meeting on `host`: both writers open, both "previous" capture threads finished,
    and PortAudio re-inits returning `host` (mutate it to change the device list)."""
    recorder = Recorder(tmp_path, mic_device_name=mic_device_name, system_device_name=system_device_name)
    recorder._started_at = 0.0
    recorder._mic_writer = _SegmentedWavWriter(tmp_path / "mic.wav", 1, SAMPLE_WIDTH_BYTES, 16000)
    recorder._system_writer = _SegmentedWavWriter(tmp_path / "system.wav", 2, SAMPLE_WIDTH_BYTES, 48000)
    recorder._mic_thread = _finished_thread()
    recorder._system_thread = _finished_thread()
    recorder._mic_active_device_name = "Built-in Mic"
    recorder._system_active_device_name = "Speakers [Loopback]"
    recorder._pyaudio = host
    recorder._pyaudio_module = _ReloadableAudio(host)
    return recorder


class _ReloadHost(_FakeAudioHost):
    """_FakeAudioHost plus open()/terminate(); every opened stream stops its thread straight away."""

    def __init__(self, devices, default_input, loopbacks, default_output_index):
        super().__init__(devices, default_input, loopbacks, default_output_index)
        self.terminated = 0
        self.recorder = None

    def open(self, **kwargs):
        index = kwargs["input_device_index"]
        event = (
            self.recorder._mic_stop_event
            if any(d["index"] == index for d in self._devices if not d.get("isLoopbackDevice"))
            else self.recorder._system_stop_event
        )
        return _FakeStream([], stop_event=event)

    def terminate(self):
        self.terminated += 1


def _loopback(name, index):
    return {"name": name, "index": index, "maxInputChannels": 2, "defaultSampleRate": 48000,
            "isLoopbackDevice": True}


def _mic(name, index):
    return {"name": name, "index": index, "maxInputChannels": 1, "defaultSampleRate": 16000}


def test_reload_devices_picks_up_a_newly_connected_default_output(tmp_path):
    # The case this exists for: headphones plugged in mid-meeting become the Windows default. PortAudio's
    # device list from when the meeting started has never heard of them; only a re-init does.
    built_in = _mic("Built-in Mic", 0)
    speakers = {"name": "Speakers", "index": 1, "maxInputChannels": 0, "defaultSampleRate": 48000}
    host = _ReloadHost([built_in, speakers], built_in, [_loopback("Speakers [Loopback]", 3)], 1)
    recorder = _reload_ready_recorder(tmp_path, host)
    host.recorder = recorder

    headphones = {"name": "Headphones", "index": 2, "maxInputChannels": 0, "defaultSampleRate": 44100}
    host._devices.append(headphones)
    host._loopbacks.append({**_loopback("Headphones [Loopback]", 4), "defaultSampleRate": 44100})
    host._default_output_index = 2

    recorder.reload_devices(reason="audio devices changed")
    recorder._mic_thread.join(timeout=5)
    recorder._system_thread.join(timeout=5)

    assert host.terminated == 1 and recorder._pyaudio_module.created == 1
    assert recorder._system_active_device_name == "Headphones [Loopback]"
    assert recorder.capture_notices() == (
        "System audio switched to 'Headphones [Loopback]' — audio devices changed.",
    )
    # Both streams restarted into new parts; the system one at the new device's sample rate.
    assert [p.name for p in recorder._system_writer.paths] == ["system.wav", "system.part2.wav"]
    with contextlib.closing(wave.open(str(tmp_path / "system.part2.wav"), "rb")) as part:
        assert part.getframerate() == 44100
    assert recorder.device_list_version == 1
    assert [d.name for d in recorder.available_devices()[1]] == [
        "Speakers [Loopback]", "Headphones [Loopback]",
    ]


def test_reload_devices_returns_to_an_explicitly_chosen_device_once_it_is_back(tmp_path):
    built_in = _mic("Built-in Mic", 0)
    speakers = {"name": "Speakers", "index": 1, "maxInputChannels": 0, "defaultSampleRate": 48000}
    host = _ReloadHost([built_in, speakers], built_in, [_loopback("Speakers [Loopback]", 3)], 1)
    recorder = _reload_ready_recorder(tmp_path, host, mic_device_name="Jabra Evolve")
    host.recorder = recorder

    host._devices.append(_mic("Jabra Evolve", 2))
    recorder.reload_devices()
    recorder._mic_thread.join(timeout=5)
    recorder._system_thread.join(timeout=5)

    assert recorder._mic_active_device_name == "Jabra Evolve"
    assert recorder._system_active_device_name == "Speakers [Loopback]"  # unchanged, so no notice for it
    assert recorder.capture_notices() == (
        "Microphone switched to 'Jabra Evolve' — the device list was refreshed.",
    )


def test_reload_devices_keeps_portaudio_when_a_capture_thread_is_stuck(tmp_path, monkeypatch):
    # Same rule as stop(): never terminate PortAudio under a thread still inside a native read.
    built_in = _mic("Built-in Mic", 0)
    speakers = {"name": "Speakers", "index": 1, "maxInputChannels": 0, "defaultSampleRate": 48000}
    host = _ReloadHost([built_in, speakers], built_in, [_loopback("Speakers [Loopback]", 3)], 1)
    recorder = _reload_ready_recorder(tmp_path, host)
    host.recorder = recorder
    monkeypatch.setattr(Recorder, "_join_capture_thread", staticmethod(lambda thread, timeout=5.0: True))

    recorder.reload_devices()
    recorder._mic_thread.join(timeout=5)
    recorder._system_thread.join(timeout=5)

    assert host.terminated == 0 and recorder._pyaudio_module.created == 0
    assert any("Couldn't reload the audio device list" in n for n in recorder.capture_notices())


def test_the_watcher_reloads_devices_when_the_device_signature_changes(tmp_path):
    built_in = _mic("Built-in Mic", 0)
    speakers = {"name": "Speakers", "index": 1, "maxInputChannels": 0, "defaultSampleRate": 48000}
    host = _ReloadHost([built_in, speakers], built_in, [_loopback("Speakers [Loopback]", 3)], 1)
    recorder = _reload_ready_recorder(tmp_path, host)
    host.recorder = recorder
    signatures = iter([("before",), ("before",), ("after",)])
    recorder._device_signature = lambda: next(signatures, ("after",))

    _run_watcher_until(recorder, lambda: recorder.device_list_version >= 1)

    assert recorder.device_list_version == 1  # reloaded once for the one change, not on every tick


def test_the_watcher_does_nothing_while_devices_are_unchanged_or_unknown(tmp_path):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    signatures = iter([("same",), None, ("same",), None])
    recorder._device_signature = lambda: next(signatures, ("same",))
    # No PortAudio is wired up at all — any reload attempt would surface as a failure notice.

    _run_watcher_until(recorder, lambda: False, timeout=0.15)

    assert recorder.device_list_version == 0
    assert recorder.capture_notices() == ()


def test_the_watcher_survives_a_signature_check_that_raises(tmp_path):
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0

    def boom():
        raise OSError("winmm unavailable")

    recorder._device_signature = boom
    _run_watcher_until(recorder, lambda: False, timeout=0.1)
    assert recorder.capture_notices() == ()


def test_reload_devices_reports_portaudio_failing_to_restart_and_can_try_again(tmp_path):
    built_in = _mic("Built-in Mic", 0)
    speakers = {"name": "Speakers", "index": 1, "maxInputChannels": 0, "defaultSampleRate": 48000}
    host = _ReloadHost([built_in, speakers], built_in, [_loopback("Speakers [Loopback]", 3)], 1)
    recorder = _reload_ready_recorder(tmp_path, host)
    host.recorder = recorder
    working_pyaudio = recorder._pyaudio_module.PyAudio

    def broken():
        raise OSError("PortAudio init failed")

    recorder._pyaudio_module.PyAudio = broken
    recorder.reload_devices()
    assert recorder._pyaudio is None
    assert any("Couldn't restart audio" in n and "OSError" in n for n in recorder.capture_notices())

    recorder._pyaudio_module.PyAudio = working_pyaudio
    recorder.reload_devices()
    recorder._mic_thread.join(timeout=5)
    recorder._system_thread.join(timeout=5)
    assert recorder._pyaudio is host
    assert recorder.device_list_version == 1


# --- a stream that never delivers anything -----------------------------------------------------------


def _active_health() -> StreamHealth:
    return StreamHealth(
        seconds_captured=60.0,
        peak_dbfs=-20.0,
        heard_activity=True,
        seconds_since_activity=1.0,
        digital_silence=False,
        clipping=False,
    )


def test_an_open_stream_that_has_delivered_nothing_reports_how_long_it_has_waited():
    monitor = _StreamActivityMonitor()
    assert monitor.health(100.0).seconds_without_data is None  # not opened yet — nothing to conclude

    monitor.stream_opened(100.0)

    assert monitor.health(125.0).seconds_without_data == 25.0


def test_waiting_ends_once_a_chunk_arrives_and_restarts_when_the_stream_is_reopened():
    monitor = _StreamActivityMonitor()
    monitor.stream_opened(100.0)
    monitor.observe(_ChunkStats(level=0.1, rms_dbfs=-40.0, all_zero=False, clipped=False), 101.0)
    assert monitor.health(150.0).seconds_without_data is None

    monitor.stream_opened(160.0)  # a device switch onto a device that delivers nothing

    assert monitor.health(200.0).seconds_without_data == 40.0


def test_system_audio_delivering_nothing_while_the_mic_is_active_is_reported():
    # The field case: the call played through a headset while a different output was being recorded.
    waiting = replace(StreamHealth.idle(), seconds_without_data=1600.0)

    (problem,) = describe_input_problems(_active_health(), waiting)

    assert problem.startswith("System audio: nothing at all has come through in 1600s")
    assert "different one" in problem


def test_no_data_is_not_reported_before_the_grace_period_or_while_the_other_track_is_quiet():
    too_soon = replace(StreamHealth.idle(), seconds_without_data=10.0)
    assert describe_input_problems(_active_health(), too_soon) == ()

    long_wait = replace(StreamHealth.idle(), seconds_without_data=600.0)
    assert describe_input_problems(StreamHealth.idle(), long_wait) == ()
