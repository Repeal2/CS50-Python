import contextlib
import json
import math
import struct
import wave

from meeting_scribe.audio import archive
from meeting_scribe.audio.archive import archive_recording
from meeting_scribe.audio.recorder import discover_recording_parts, load_part_offsets, part_offsets_path
from meeting_scribe.transcription.engine import audio_duration_seconds


def _write_tone(path, seconds, channels=2, framerate=48000):
    frame = lambda i: struct.pack("<h", int(6000 * math.sin(i / 15))) * channels  # noqa: E731
    with contextlib.closing(wave.open(str(path), "wb")) as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(framerate)
        wav_file.writeframes(b"".join(frame(i) for i in range(int(framerate * seconds))))


def test_each_part_is_replaced_by_a_much_smaller_opus_copy_of_the_same_length(tmp_path):
    first, second = tmp_path / "system.wav", tmp_path / "system.part2.wav"
    _write_tone(first, seconds=3.0)
    _write_tone(second, seconds=2.0)

    result = archive_recording([first, second])

    assert result.archived == [tmp_path / "system.opus", tmp_path / "system.part2.opus"]
    assert not first.exists() and not second.exists()
    assert abs(audio_duration_seconds(tmp_path / "system.opus") - 3.0) < 0.1
    assert abs(audio_duration_seconds(tmp_path / "system.part2.opus") - 2.0) < 0.1
    assert result.bytes_after * 20 < result.bytes_before
    assert result.summary().startswith("Recording compressed for keeping: ")
    assert list(tmp_path.glob("*.partial")) == []


def test_archived_parts_are_found_again_in_order_with_their_start_times(tmp_path):
    _write_tone(tmp_path / "mic.wav", seconds=1.0, channels=1)
    _write_tone(tmp_path / "mic.part2.wav", seconds=1.0, channels=1)
    part_offsets_path(tmp_path / "mic.wav").write_text(
        json.dumps({"parts": [{"file": "mic.wav", "start_seconds": 0.0}, {"file": "mic.part2.wav", "start_seconds": 95.5}]}),
        encoding="utf-8",
    )

    archive_recording(discover_recording_parts(tmp_path / "mic.wav"))

    assert discover_recording_parts(tmp_path / "mic.wav") == (tmp_path / "mic.opus", tmp_path / "mic.part2.opus")
    assert load_part_offsets(tmp_path / "mic.wav") == {tmp_path / "mic.opus": 0.0, tmp_path / "mic.part2.opus": 95.5}


def test_a_part_that_fails_to_compress_is_kept_as_recorded(tmp_path, monkeypatch):
    wav = tmp_path / "mic.wav"
    _write_tone(wav, seconds=1.0, channels=1)

    def broken(source, target):
        target.write_bytes(b"half an opus file")
        raise OSError("No space left on device")

    monkeypatch.setattr(archive, "encode_opus", broken)

    result = archive_recording([wav])

    assert wav.exists()
    assert result.archived == []
    assert list(tmp_path.glob("*.opus*")) == []  # the half-written copy is cleaned up
    assert result.summary() == "mic.wav was kept uncompressed: No space left on device"


def test_a_copy_that_comes_out_the_wrong_length_is_not_kept(tmp_path, monkeypatch):
    wav, short = tmp_path / "mic.wav", tmp_path / "short.wav"
    _write_tone(wav, seconds=3.0, channels=1)
    _write_tone(short, seconds=1.0, channels=1)
    real_encode = archive.encode_opus
    monkeypatch.setattr(archive, "encode_opus", lambda source, target: real_encode(short, target))

    result = archive_recording([wav])

    assert wav.exists()
    assert "kept uncompressed: the compressed copy runs 1.0 s against the original's 3.0 s" in result.summary()


def test_empty_and_already_compressed_parts_are_left_alone(tmp_path):
    (tmp_path / "system.wav").write_bytes(b"")  # a device that never delivered anything
    (tmp_path / "mic.opus").write_bytes(b"already done")

    result = archive_recording([tmp_path / "system.wav", tmp_path / "mic.opus"])

    assert (tmp_path / "system.wav").exists() and (tmp_path / "mic.opus").read_bytes() == b"already done"
    assert result.summary() is None
