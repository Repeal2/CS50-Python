import contextlib
import io
import math
import struct
import wave

import pytest

av = pytest.importorskip("av")  # a faster-whisper dependency — present wherever the app runs

from meeting_scribe.transcription.speech_encoding import encode_speech_chunks  # noqa: E402


def _write_tone_wav(path, seconds, framerate=48000, channels=2):
    """A 440 Hz tone in the loopback recorder's usual format, so there's real signal to encode."""
    frames = bytearray()
    for i in range(int(framerate * seconds)):
        sample = int(8000 * math.sin(2 * math.pi * 440 * i / framerate))
        frames += struct.pack("<h", sample) * channels
    with contextlib.closing(wave.open(str(path), "wb")) as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(framerate)
        wav_file.writeframes(bytes(frames))


def _decode(data):
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        frames = list(container.decode(audio=0))
        return stream.codec_context.name, stream.layout.name, frames, stream.rate


def test_a_short_recording_becomes_one_small_opus_chunk(tmp_path):
    path = tmp_path / "system.wav"
    _write_tone_wav(path, seconds=3.0)

    (chunk,) = encode_speech_chunks(path, max_chunk_seconds=60)

    assert (chunk.start_seconds, chunk.duration_seconds) == (0.0, 3.0)
    assert chunk.data[:4] == b"OggS"
    assert chunk.mime_type == "audio/ogg"
    codec, layout, frames, rate = _decode(chunk.data)
    assert (codec, layout) == ("opus", "mono")
    assert sum(frame.samples for frame in frames) / rate == pytest.approx(3.0, abs=0.05)
    # ~16 kbps vs the WAV's ~1.5 Mbps.
    assert len(chunk.data) < path.stat().st_size / 50


def test_a_long_recording_is_split_into_consecutive_standalone_chunks(tmp_path):
    path = tmp_path / "system.wav"
    _write_tone_wav(path, seconds=2.5)

    chunks = encode_speech_chunks(path, max_chunk_seconds=1.0)

    assert [(c.start_seconds, c.duration_seconds) for c in chunks] == [(0.0, 1.0), (1.0, 1.0), (2.0, 0.5)]
    for chunk in chunks:
        _codec, _layout, frames, rate = _decode(chunk.data)
        # Every chunk is its own file starting at zero, not a slice claiming to begin mid-meeting.
        assert frames[0].pts == 0
        assert sum(frame.samples for frame in frames) / rate == pytest.approx(chunk.duration_seconds, abs=0.05)


def test_an_empty_recording_gives_no_chunks(tmp_path):
    path = tmp_path / "system.wav"
    _write_tone_wav(path, seconds=0)

    assert encode_speech_chunks(path, max_chunk_seconds=60) == []


def test_each_later_chunk_repeats_the_end_of_the_one_before(tmp_path):
    path = tmp_path / "system.wav"
    _write_tone_wav(path, seconds=2.5)

    chunks = encode_speech_chunks(path, max_chunk_seconds=1.0, overlap_seconds=0.3)

    assert [(c.start_seconds, c.duration_seconds, c.overlap_seconds) for c in chunks] == [
        (0.0, 1.0, 0.0),
        (0.7, 1.3, 0.3),
        (1.7, 0.8, 0.3),
    ]
    for chunk in chunks:
        _codec, _layout, frames, rate = _decode(chunk.data)
        assert frames[0].pts == 0
        assert sum(frame.samples for frame in frames) / rate == pytest.approx(chunk.duration_seconds, abs=0.05)
