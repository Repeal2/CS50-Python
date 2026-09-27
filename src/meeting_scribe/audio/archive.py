"""Shrinks a finished meeting's recording once it has been transcribed.

The recorder writes WAV at each device's own format — typically 48 kHz stereo 16-bit, about 0.7 GB an hour
per track — because that's the safe way to capture (see audio.recorder). Once the meeting has its
transcript, though, the recording is only kept for listening back and for transcribing again, and Opus at
32 kbps, 16 kHz mono holds everything either of those uses: Whisper and WhisperX both work at 16 kHz mono
anyway. That's about 14 MB an hour per track, some fifty times smaller.

Each WAV part becomes an Ogg/Opus file of the same name with an ".opus" suffix ("mic.part2.wav" ->
"mic.part2.opus"). The WAV is only deleted once its Opus copy has been written in full under a temporary
name, read back, found to run as long as the original, and renamed into place — so a crash or a full disk
partway through leaves the WAV, never a meeting with neither. Every step reads parts the same way whichever
form they're in (see recorder.discover_recording_parts and transcription.engine.audio_duration_seconds),
so retrying or re-transcribing an archived meeting works exactly as before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

ARCHIVE_SUFFIX = ".opus"
_SAMPLE_RATE = 16000
_BIT_RATE = 32000
# An Opus copy is kept only if it runs this close to the WAV's length. Opus pads the final frame, and
# resampling rounds; anything further off means the encode went wrong.
_DURATION_TOLERANCE_SECONDS = 0.5


@dataclass
class ArchiveResult:
    archived: list[Path] = field(default_factory=list)  # the Opus files written
    bytes_before: int = 0
    bytes_after: int = 0
    problems: list[str] = field(default_factory=list)  # ready-to-show, one per part left as WAV

    def summary(self) -> str | None:
        """One line for the meeting's log, or None if there was nothing to compress."""
        lines = []
        if self.archived:
            lines.append(
                f"Recording compressed for keeping: {_megabytes(self.bytes_before)} -> "
                f"{_megabytes(self.bytes_after)}."
            )
        lines.extend(self.problems)
        return " ".join(lines) or None


def _megabytes(count: int) -> str:
    return f"{count / 1024**2:,.1f} MB"


def archive_path(wav_path: Path) -> Path:
    return Path(wav_path).with_suffix(ARCHIVE_SUFFIX)


def archive_recording(wav_paths: list[Path] | tuple[Path, ...]) -> ArchiveResult:
    """Compresses each WAV in `wav_paths` to Opus beside it and removes the WAV (see the module
    docstring). Anything that isn't a WAV — a part already archived — is skipped. A part that can't be
    compressed is left as it is and said so in `problems`; this never raises, because by the time it runs
    the meeting is already saved and nothing here is worth failing it over."""
    from meeting_scribe.transcription.engine import audio_duration_seconds

    result = ArchiveResult()
    for wav_path in wav_paths:
        wav_path = Path(wav_path)
        if wav_path.suffix.lower() != ".wav" or not wav_path.exists():
            continue
        target = archive_path(wav_path)
        temporary = target.with_name(target.name + ".partial")
        try:
            wav_size = wav_path.stat().st_size
            wav_seconds = audio_duration_seconds(wav_path)
            if wav_seconds <= 0:
                continue  # an empty part holds nothing worth keeping either way; leave it be
            encode_opus(wav_path, temporary)
            opus_seconds = audio_duration_seconds(temporary)
            if abs(opus_seconds - wav_seconds) > _DURATION_TOLERANCE_SECONDS:
                raise ValueError(
                    f"the compressed copy runs {opus_seconds:.1f} s against the original's {wav_seconds:.1f} s"
                )
            os.replace(temporary, target)
            wav_path.unlink()
        except Exception as error:
            temporary.unlink(missing_ok=True)
            result.problems.append(f"{wav_path.name} was kept uncompressed: {error}")
            continue
        result.archived.append(target)
        result.bytes_before += wav_size
        result.bytes_after += target.stat().st_size
    return result


def encode_opus(wav_path: Path, target: Path) -> None:
    """Writes `wav_path` to `target` as 16 kHz mono Ogg/Opus, streaming, so memory stays small however
    long the recording is."""
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=_SAMPLE_RATE)
    with av.open(str(wav_path)) as source, av.open(str(target), mode="w", format="ogg") as output:
        stream = output.add_stream("libopus", rate=_SAMPLE_RATE, layout="mono")
        stream.bit_rate = _BIT_RATE
        samples = 0

        def write(frames) -> None:
            nonlocal samples
            for frame in frames:
                frame.pts = samples
                frame.time_base = Fraction(1, _SAMPLE_RATE)
                samples += frame.samples
                for packet in stream.encode(frame):
                    output.mux(packet)

        for frame in source.decode(audio=0):
            write(resampler.resample(frame))
        write(resampler.resample(None))
        for packet in stream.encode(None):
            output.mux(packet)
