"""IsolatedWhisperTranscriber with a real child process, running stand-in entry points below instead of
the real model (spawn imports this module in the child to find them)."""

import os
from pathlib import Path

import pytest

from meeting_scribe.transcription import worker
from meeting_scribe.transcription.engine import TranscriptLine, WhisperTranscriber
from meeting_scribe.transcription.worker import IsolatedWhisperTranscriber, TranscriptionFailed


def _answers(connection, model_size, crash_log):
    """Transcribes anything as one line naming the model and the file, reporting progress on the way."""
    while True:
        request = connection.recv()
        if request is None:
            return
        paths, source, offsets, language, vocabulary = request
        connection.send(("progress", 1.5))
        connection.send(("done", [TranscriptLine(0.0, source, f"{model_size}:{Path(paths[0]).name}:{language}")]))


def _crashes(connection, model_size, crash_log):
    """What an out-of-memory abort inside native code looks like from outside: the process just ends."""
    connection.recv()
    os._exit(3)


def _raises(connection, model_size, crash_log):
    connection.recv()
    connection.send(("error", "ValueError: unreadable audio"))


def test_lines_and_progress_come_back_from_the_child_process():
    transcriber = IsolatedWhisperTranscriber("tiny", target=_answers)
    progress = []
    try:
        lines = transcriber.transcribe_parts([Path("mic.wav")], "mic", on_progress=progress.append, language="en")
    finally:
        transcriber.unload()

    assert lines == [TranscriptLine(0.0, "mic", "tiny:mic.wav:en")]
    assert progress == [1.5]


def test_a_crashed_child_fails_the_transcription_not_the_app():
    transcriber = IsolatedWhisperTranscriber("tiny", target=_crashes)

    with pytest.raises(TranscriptionFailed, match=r"stopped unexpectedly \(exit code 3\).*Retry"):
        transcriber.transcribe_parts([Path("mic.wav")], "mic")
    transcriber.unload()  # still safe after the process is gone


def test_an_error_in_the_child_is_raised_with_its_message():
    transcriber = IsolatedWhisperTranscriber("tiny", target=_raises)
    try:
        with pytest.raises(TranscriptionFailed, match="ValueError: unreadable audio"):
            transcriber.transcribe_parts([Path("mic.wav")], "mic")
    finally:
        transcriber.unload()


def test_the_child_is_kept_between_tracks_and_ended_by_unload():
    transcriber = IsolatedWhisperTranscriber("tiny", target=_answers)
    transcriber.transcribe_parts([Path("mic.wav")], "mic")
    process = transcriber._process
    transcriber.transcribe_parts([Path("system.wav")], "system")
    assert transcriber._process is process  # the model is loaded once per meeting, not once per track

    transcriber.unload()

    assert not process.is_alive()
    assert transcriber._process is None


class _FakeConnection:
    def __init__(self, *requests):
        self.requests, self.sent = list(requests), []

    def recv(self):
        if not self.requests:
            raise EOFError
        return self.requests.pop(0)

    def send(self, message):
        self.sent.append(message)


def test_serve_runs_each_request_through_the_real_transcriber(monkeypatch):
    def fake_transcribe_parts(self, paths, source, start_offsets=None, on_progress=None, **hints):
        on_progress(2.0)
        return [TranscriptLine(start_offsets[paths[0]], source, f"{hints['language']} {hints['vocabulary']}")]

    monkeypatch.setattr(WhisperTranscriber, "transcribe_parts", fake_transcribe_parts)
    connection = _FakeConnection(((Path("mic.wav"),), "mic", {Path("mic.wav"): 4.0}, "en", "Acme"), None)

    worker._serve(connection, "tiny", None)

    assert connection.sent == [("progress", 2.0), ("done", [TranscriptLine(4.0, "mic", "en Acme")])]


def test_serve_reports_an_error_and_keeps_serving(monkeypatch):
    def failing(self, *args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(WhisperTranscriber, "transcribe_parts", failing)
    request = ((Path("mic.wav"),), "mic", None, None, None)
    connection = _FakeConnection(request, request)  # then EOF: the parent went away

    worker._serve(connection, "tiny", None)

    assert connection.sent == [("error", "OSError: disk gone")] * 2
