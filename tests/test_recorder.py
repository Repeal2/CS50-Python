import struct
import sys

import pytest

from meeting_scribe.audio.recorder import Recorder, _pcm16_level


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
