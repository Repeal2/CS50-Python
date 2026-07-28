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
