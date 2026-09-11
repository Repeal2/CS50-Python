"""Local speech-to-text (faster-whisper) plus the merge that turns two audio tracks and a screen-OCR
event stream into one time-ordered meeting transcript.
"""

from __future__ import annotations

import contextlib
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

SOURCE_LABELS = {"mic": "You", "system": "Others", "screen_ocr": "Screen"}


@dataclass(frozen=True)
class TranscriptLine:
    timestamp_seconds: float
    source: str  # "mic" | "system" | "screen_ocr"
    text: str


class WhisperTranscriber:
    """Thin wrapper around faster-whisper. The model is loaded lazily on first use so importing this
    module (or even constructing a transcriber) doesn't require the model weights to be present."""

    def __init__(self, model_size: str = "small"):
        self._model_size = model_size
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            # device="cpu" is deliberate: with no device specified, ctranslate2 auto-detects and will
            # try CUDA on any machine with an NVIDIA GPU, then fail trying to load cuBLAS — we don't
            # bundle the CUDA runtime (it would add gigabytes for a benefit most users can't use), so
            # CPU is the only device this app actually supports.
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, audio_path: Path, source: str) -> list[TranscriptLine]:
        """Runs Whisper over a recorded WAV file, returning one TranscriptLine per detected segment."""
        model = self._ensure_model()
        segments, _info = model.transcribe(str(audio_path), vad_filter=True)
        return [
            TranscriptLine(timestamp_seconds=segment.start, source=source, text=segment.text.strip())
            for segment in segments
            if segment.text.strip()
        ]

    def transcribe_parts(self, audio_paths: Sequence[Path], source: str) -> list[TranscriptLine]:
        """Transcribes one capture stream that may have been written as several WAV parts.

        A WAV file can't hold more than a couple of hours of audio before its header runs out of room,
        so a long meeting rolls over into numbered parts (see audio.recorder). Whisper timestamps each
        part from its own zero; shifting every part by the total duration of the ones before it puts
        the whole stream back on the meeting's clock, which is what the merge into one transcript
        assumes."""
        lines: list[TranscriptLine] = []
        offset_seconds = 0.0
        for audio_path in audio_paths:
            lines.extend(
                TranscriptLine(line.timestamp_seconds + offset_seconds, line.source, line.text)
                for line in self.transcribe(audio_path, source=source)
            )
            offset_seconds += wav_duration_seconds(audio_path)
        return lines


def wav_duration_seconds(path: Path) -> float:
    """Length of a WAV file in seconds, or 0.0 if it can't be read — a missing or unreadable part
    shouldn't sink a transcript that otherwise came out fine."""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wav_file:
            framerate = wav_file.getframerate()
            return wav_file.getnframes() / framerate if framerate else 0.0
    except (OSError, wave.Error):
        return 0.0


def merge_transcript_lines(*line_groups: list[TranscriptLine]) -> list[TranscriptLine]:
    """Interleaves mic / system / screen-OCR lines into one chronological transcript."""
    merged: list[TranscriptLine] = [line for group in line_groups for line in group]
    merged.sort(key=lambda line: line.timestamp_seconds)
    return merged


def render_transcript(lines: list[TranscriptLine]) -> str:
    """Renders merged transcript lines as readable `[mm:ss] Speaker: text` rows."""
    rows = []
    for line in lines:
        minutes, seconds = divmod(max(0, int(line.timestamp_seconds)), 60)
        label = SOURCE_LABELS.get(line.source, line.source)
        rows.append(f"[{minutes:02d}:{seconds:02d}] {label}: {line.text}")
    return "\n".join(rows)
