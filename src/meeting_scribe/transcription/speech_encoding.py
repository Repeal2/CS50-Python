"""Compresses a recorded WAV into small Ogg/Opus chunks for sending to a transcription service.

The recorder writes system audio at the output device's native format — typically 48 kHz stereo 16-bit,
about 190 KB a second — so even a one-minute meeting is over the 10 MB a single Runpod request can carry
(see runpod_whisperx). Speech recognition and diarization models work at 16 kHz mono anyway, so
converting to that and encoding as 16 kbps Opus (a codec built for speech) shrinks it roughly a
hundredfold without losing anything they'd use. Long recordings are still split into chunks of at most
`max_chunk_seconds`, each a self-contained Ogg file whose timestamps start at zero.

Uses PyAV, which faster-whisper already depends on, so this adds no new dependency.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

SAMPLE_RATE = 16000
BIT_RATE = 16000
# 20 ms at 16 kHz — libopus' default frame duration. Opus only accepts a few fixed frame sizes, so audio
# is fed to it in exactly this many samples at a time (plus one shorter final frame, which it pads).
_FRAME_SAMPLES = 320


@dataclass(frozen=True)
class EncodedChunk:
    start_seconds: float  # where this chunk starts within the source file
    duration_seconds: float
    data: bytes  # a complete Ogg/Opus file

    @property
    def mime_type(self) -> str:
        return "audio/ogg"


class _ChunkEncoder:
    def __init__(self, start_sample: int):
        import av

        self.start_sample = start_sample
        self.samples = 0
        self._buffer = io.BytesIO()
        self._container = av.open(self._buffer, mode="w", format="ogg")
        self._stream = self._container.add_stream("libopus", rate=SAMPLE_RATE, layout="mono")
        self._stream.bit_rate = BIT_RATE

    def encode(self, frame) -> None:
        # Timestamps restart at zero in every chunk, so each is a normal standalone file rather than one
        # that claims to begin partway through the meeting.
        frame.pts = self.samples
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self.samples += frame.samples
        for packet in self._stream.encode(frame):
            self._container.mux(packet)

    def finish(self) -> EncodedChunk:
        for packet in self._stream.encode(None):
            self._container.mux(packet)
        self._container.close()
        return EncodedChunk(
            start_seconds=self.start_sample / SAMPLE_RATE,
            duration_seconds=self.samples / SAMPLE_RATE,
            data=self._buffer.getvalue(),
        )


def encode_speech_chunks(wav_path: Path, *, max_chunk_seconds: float) -> list[EncodedChunk]:
    """Decodes `wav_path`, converts it to 16 kHz mono, and encodes it as Opus in consecutive chunks of at
    most `max_chunk_seconds` each. Streams through the file, so memory use stays small however long the
    recording is. An empty recording gives an empty list."""
    import av

    samples_per_chunk = max(1, round(max_chunk_seconds * SAMPLE_RATE) // _FRAME_SAMPLES) * _FRAME_SAMPLES
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    fifo = av.AudioFifo()
    chunks: list[EncodedChunk] = []
    current: _ChunkEncoder | None = None
    total_samples = 0

    def encode(frame) -> None:
        nonlocal current, total_samples
        if current is None:
            current = _ChunkEncoder(total_samples)
        total_samples += frame.samples
        current.encode(frame)
        if current.samples >= samples_per_chunk:
            chunks.append(current.finish())
            current = None

    def drain(final: bool) -> None:
        while fifo.samples >= _FRAME_SAMPLES:
            encode(fifo.read(_FRAME_SAMPLES))
        if final and fifo.samples:
            encode(fifo.read())

    with av.open(str(wav_path)) as source:
        for frame in source.decode(audio=0):
            for resampled in resampler.resample(frame):
                fifo.write(resampled)
            drain(final=False)
    for resampled in resampler.resample(None):
        fifo.write(resampled)
    drain(final=True)
    if current is not None:
        chunks.append(current.finish())
    return chunks
