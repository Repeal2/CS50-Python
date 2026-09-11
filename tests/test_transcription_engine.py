import contextlib
import sys
import wave
from types import ModuleType

from meeting_scribe.transcription.engine import (
    TranscriptLine,
    WhisperTranscriber,
    merge_transcript_lines,
    render_transcript,
    wav_duration_seconds,
)


def test_constructing_transcriber_does_not_load_model():
    # No faster-whisper model weights are available in this environment; constructing the wrapper
    # must not try to load anything until .transcribe() is actually called.
    transcriber = WhisperTranscriber(model_size="tiny")
    assert transcriber._model is None


def test_ensure_model_forces_cpu_device(monkeypatch):
    # Regression test: with no device argument, ctranslate2 auto-detects and tries CUDA on any machine
    # with an NVIDIA GPU, then crashes trying to load cublas64_12.dll since we don't bundle the CUDA
    # runtime. device="cpu" must always be passed explicitly.
    captured_kwargs = {}

    class FakeWhisperModel:
        def __init__(self, model_size_or_path, **kwargs):
            captured_kwargs["model_size_or_path"] = model_size_or_path
            captured_kwargs.update(kwargs)

    fake_module = ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    transcriber = WhisperTranscriber(model_size="small")
    transcriber._ensure_model()

    assert captured_kwargs["model_size_or_path"] == "small"
    assert captured_kwargs["device"] == "cpu"


def test_merge_interleaves_by_timestamp():
    mic = [TranscriptLine(2.0, "mic", "go ahead")]
    system = [TranscriptLine(0.5, "system", "can you hear me")]
    screen = [TranscriptLine(1.0, "screen_ocr", "Slide: Agenda")]

    merged = merge_transcript_lines(mic, system, screen)

    assert [line.text for line in merged] == ["can you hear me", "Slide: Agenda", "go ahead"]


def test_render_transcript_formats_timestamps_and_labels():
    lines = [
        TranscriptLine(0.0, "mic", "hello"),
        TranscriptLine(65.0, "system", "hi there"),
        TranscriptLine(70.0, "screen_ocr", "Q3 Roadmap"),
    ]

    text = render_transcript(lines)

    assert text == (
        "[00:00] You: hello\n"
        "[01:05] Others: hi there\n"
        "[01:10] Screen: Q3 Roadmap"
    )


def _write_wav(path, seconds, framerate=16000):
    with contextlib.closing(wave.open(str(path), "wb")) as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(framerate)
        wav_file.writeframes(b"\x00\x00" * int(framerate * seconds))


def test_wav_duration_seconds_reads_the_header(tmp_path):
    _write_wav(tmp_path / "mic.wav", seconds=2.5)
    assert wav_duration_seconds(tmp_path / "mic.wav") == 2.5


def test_wav_duration_seconds_tolerates_a_file_it_cannot_read(tmp_path):
    missing = tmp_path / "nope.wav"
    not_a_wav = tmp_path / "junk.wav"
    not_a_wav.write_bytes(b"not audio")

    assert wav_duration_seconds(missing) == 0.0
    assert wav_duration_seconds(not_a_wav) == 0.0


def test_transcribe_parts_puts_every_part_back_on_the_meetings_clock(tmp_path, monkeypatch):
    # A meeting long enough to fill a WAV header rolls over into further part files (see
    # audio.recorder). Whisper timestamps each part from its own zero, so without the offset every
    # part after the first would land back at the start of the transcript.
    first, second = tmp_path / "mic.wav", tmp_path / "mic.part2.wav"
    _write_wav(first, seconds=60.0)
    _write_wav(second, seconds=30.0)

    def fake_transcribe(self, audio_path, source):
        said = "first part" if audio_path == first else "second part"
        return [TranscriptLine(5.0, source, said)]

    monkeypatch.setattr(WhisperTranscriber, "transcribe", fake_transcribe)

    lines = WhisperTranscriber(model_size="tiny").transcribe_parts([first, second], source="mic")

    assert [(line.timestamp_seconds, line.source, line.text) for line in lines] == [
        (5.0, "mic", "first part"),
        (65.0, "mic", "second part"),
    ]


def test_transcribe_parts_of_a_single_file_leaves_timestamps_alone(tmp_path, monkeypatch):
    only = tmp_path / "mic.wav"
    _write_wav(only, seconds=10.0)
    monkeypatch.setattr(
        WhisperTranscriber,
        "transcribe",
        lambda self, audio_path, source: [TranscriptLine(3.0, source, "hello")],
    )

    lines = WhisperTranscriber(model_size="tiny").transcribe_parts([only], source="mic")

    assert [(line.timestamp_seconds, line.text) for line in lines] == [(3.0, "hello")]
