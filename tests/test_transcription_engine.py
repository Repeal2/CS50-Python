from meeting_scribe.transcription.engine import (
    TranscriptLine,
    WhisperTranscriber,
    merge_transcript_lines,
    render_transcript,
)


def test_constructing_transcriber_does_not_load_model():
    # No faster-whisper model weights are available in this environment; constructing the wrapper
    # must not try to load anything until .transcribe() is actually called.
    transcriber = WhisperTranscriber(model_size="tiny")
    assert transcriber._model is None


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
