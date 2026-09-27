"""Runs local transcription in a child process, so a crash inside it can't take the app down.

faster-whisper's heavy lifting happens in native code (CTranslate2, Intel MKL). When that fails —
`mkl_malloc: failed to allocate memory` on a long meeting is the one seen in the field — it aborts the
whole process, with no Python exception to catch. With transcription running in the background after
Stop, that used to mean a crash while transcribing one meeting also killed the next meeting being
recorded, and the app with it. Here the model lives in a child process instead: if that dies, the parent
notices, the transcription fails like any other error (the recording stays on disk for Retry), and
whatever else the app was doing carries on.

The child is started on first use and kept for the rest of the transcription, so the model is loaded once
per meeting, and unload() ends it — which also hands every byte it held back to Windows, more reliably
than dropping the model inside a long-lived process. It's started with "spawn", the only method Windows
has, so a frozen build needs multiprocessing.freeze_support() at its entry point (see main.py).
"""

from __future__ import annotations

import faulthandler
import multiprocessing
from pathlib import Path
from typing import Callable, Mapping, Sequence

from meeting_scribe.transcription.engine import TranscriptLine, WhisperTranscriber

# How often the parent checks the child is still alive while waiting for it to say something.
_POLL_SECONDS = 0.5


class TranscriptionFailed(RuntimeError):
    """Local transcription failed, or its process stopped without finishing."""


def _serve(connection, model_size: str, crash_log: str | None) -> None:
    """The child process: loads nothing until asked, then answers transcription requests until told to
    stop (None) or the parent goes away."""
    if crash_log:
        try:
            # Kept open for the life of the process, for the same reason as main._install_crash_logging.
            faulthandler.enable(file=open(crash_log, "a", encoding="utf-8"), all_threads=True)
        except OSError:
            pass
    transcriber = WhisperTranscriber(model_size=model_size)
    while True:
        try:
            request = connection.recv()
        except EOFError:
            return  # the parent is gone
        if request is None:
            return
        audio_paths, source, start_offsets, language, vocabulary = request
        try:
            lines = transcriber.transcribe_parts(
                audio_paths,
                source,
                start_offsets=start_offsets,
                on_progress=lambda seconds: connection.send(("progress", seconds)),
                language=language,
                vocabulary=vocabulary,
            )
        except Exception as error:
            connection.send(("error", f"{type(error).__name__}: {error}"))
        else:
            connection.send(("done", lines))


class IsolatedWhisperTranscriber:
    """WhisperTranscriber's transcribe_parts and unload, with the model in a child process (see the module
    docstring). Raises TranscriptionFailed for anything that goes wrong in there, including the process
    dying."""

    def __init__(self, model_size: str = "small", *, crash_log: Path | None = None, target=_serve):
        self._model_size = model_size
        self._crash_log = str(crash_log) if crash_log else None
        self._target = target  # the child's entry point; replaceable so tests needn't load a real model
        self._process = None
        self._connection = None

    def _ensure_process(self):
        if self._process is None or not self._process.is_alive():
            context = multiprocessing.get_context("spawn")
            parent_end, child_end = context.Pipe()
            process = context.Process(
                target=self._target,
                args=(child_end, self._model_size, self._crash_log),
                name="meeting-scribe-transcriber",
                # Ends with the app: a transcription nobody is waiting for any more shouldn't hold on to
                # gigabytes of memory in the background.
                daemon=True,
            )
            process.start()
            child_end.close()
            self._process, self._connection = process, parent_end
        return self._process, self._connection

    def transcribe_parts(
        self,
        audio_paths: Sequence[Path],
        source: str,
        start_offsets: Mapping[Path, float] | None = None,
        on_progress: Callable[[float], None] | None = None,
        *,
        language: str | None = None,
        vocabulary: str | None = None,
    ) -> list[TranscriptLine]:
        process, connection = self._ensure_process()
        try:
            connection.send(
                (tuple(audio_paths), source, dict(start_offsets) if start_offsets else None, language, vocabulary)
            )
            while True:
                if not connection.poll(_POLL_SECONDS):
                    if not process.is_alive():
                        raise self._died(process)
                    continue
                kind, value = connection.recv()
                if kind == "progress":
                    if on_progress is not None:
                        on_progress(value)
                elif kind == "done":
                    return value
                else:
                    raise TranscriptionFailed(value)
        except (EOFError, BrokenPipeError, ConnectionResetError):
            # The pipe closed under us: the process died between polls.
            process.join(timeout=5)
            raise self._died(process) from None

    @staticmethod
    def _died(process) -> TranscriptionFailed:
        return TranscriptionFailed(
            f"The transcription process stopped unexpectedly (exit code {process.exitcode}) — most likely it "
            "ran out of memory. The recording is kept: close other applications or pick a smaller model in "
            "Settings, then use Retry."
        )

    def unload(self) -> None:
        """Ends the child process, releasing the model's memory. The next transcribe_parts starts a new
        one."""
        process, connection, self._process, self._connection = self._process, self._connection, None, None
        if process is None:
            return
        try:
            connection.send(None)
        except (OSError, ValueError):
            pass
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        connection.close()
