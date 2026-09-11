import contextlib
import struct
import sys
import wave
from types import SimpleNamespace

import pytest

from meeting_scribe.audio.recorder import (
    MAX_WAV_DATA_BYTES,
    SAMPLE_WIDTH_BYTES,
    Recorder,
    _pcm16_level,
    _SegmentedWavWriter,
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


def test_capture_errors_start_empty(tmp_path):
    assert Recorder(tmp_path).capture_errors() == ()


def test_a_dead_capture_thread_is_reported_rather_than_silently_losing_the_track(tmp_path):
    # The failure mode this exists for: a capture thread dying mid-meeting (a full WAV header, an
    # unplugged device, a driver error) used to leave the rest of that track silent with nothing
    # anywhere saying why — the meeting just came back missing one side of the conversation.
    recorder = Recorder(tmp_path)
    recorder._started_at = 0.0
    recorder._record_capture_error("Microphone", OSError("device disconnected"))

    assert recorder.capture_errors() == ("Microphone capture stopped early: OSError: device disconnected",)
    assert recorder.stop().errors == recorder.capture_errors()


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
        SimpleNamespace(paInt16=8), device_info, path, "mic_level", "Microphone"
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
    assert recorder.capture_errors() == ()
    assert recorder.mic_level == 0.0


def test_a_failing_capture_thread_still_leaves_a_readable_file_and_says_what_happened(tmp_path):
    # The original bug: a write that blew past the WAV header's 4 GiB ceiling killed this thread with a
    # struct.error, so the file was never finalized and nothing anywhere reported it. Whatever the
    # cause, the recording so far has to survive and the failure has to be visible.
    recorder = Recorder(tmp_path)
    stream = _FakeStream([b"\x07\x00" * 10], fail_with=OSError("device disconnected"))

    writer = _run_capture_thread(recorder, stream, tmp_path / "mic.wav")

    assert _wav_frames(writer.paths[0]) == b"\x07\x00" * 10
    assert recorder.capture_errors() == (
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
        SimpleNamespace(paInt16=8), device_info, tmp_path / "mic.wav", "mic_level", "Microphone"
    )
    thread.join(timeout=5)

    assert recorder.capture_errors() == ("Microphone capture stopped early: OSError: no such device",)
    # The empty WAV is still valid and still finalized, so transcription reads it as silence.
    assert _wav_frames(tmp_path / "mic.wav") == b""
