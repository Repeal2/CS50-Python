"""Local speech-to-text (faster-whisper) plus the merge that turns two audio tracks and a screen-OCR
event stream into one time-ordered meeting transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

            self._model = WhisperModel(self._model_size, compute_type="int8")
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
