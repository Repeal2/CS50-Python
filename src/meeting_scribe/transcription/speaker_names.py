"""Puts names to the other side's voices, using the speaker badges on Teams' live captions.

The OCR box reads Teams' live captions as the meeting goes, and each caption line sits under a badge naming
who said it (see screen.capture — every caption event remembers its badge and when it first appeared).
Those are Teams' own attributions, so they can name the transcript's lines by time: whoever the captions
say was talking while a line of system audio was spoken is who said it.

- With the cloud's speaker labels ("SPEAKER_01"), each label is matched as a whole: it takes the name the
  captions give most of the time its lines are spoken, if that's most of the captioned time for that label
  and at least a few seconds of it. Each name goes to one label at most.
- Without them (this PC's transcript), each line takes the name the captions give for most of its span.
- A name the captions mostly show while the *microphone* track is talking is the person recording, who's
  already "You", so it's never given to anyone on the other side.

Anything the captions don't clearly cover keeps the label it had — "Others", or the cloud's own label.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from meeting_scribe.transcription.engine import TranscriptLine

# Captions appear a moment after the words are said, and are only emitted once they stop growing — so a
# caption's span on the meeting's clock starts this much before it was first seen.
_CAPTION_LAG_SECONDS = 2.0
# A caption event without a first-seen time (none should lack one) is taken to cover this long before it.
_DEFAULT_CAPTION_SECONDS = 5.0
# How long a transcript line is taken to last when the next line on its track doesn't cut it shorter.
_MAX_LINE_SECONDS = 15.0
# How much of a cloud speaker label's captioned time one name needs, and how much time that has to be.
_LABEL_SHARE = 0.5
_LABEL_MIN_SECONDS = 3.0
# The same for one line of this PC's transcript.
_LINE_SHARE = 0.6
_LINE_MIN_SECONDS = 0.5


@dataclass(frozen=True)
class CaptionSpan:
    name: str
    start: float
    end: float


def caption_spans(events: Iterable) -> list[CaptionSpan]:
    """Who the captions say was talking when, from screen.capture.ScreenTextEvent-shaped events. Events
    without a speaker (slides, chat, captions with names off) are left out."""
    spans = []
    for event in events:
        name = getattr(event, "speaker", None)
        if not name:
            continue
        seen = getattr(event, "started_seconds", None)
        start = (seen if seen is not None else event.timestamp_seconds - _DEFAULT_CAPTION_SECONDS) - _CAPTION_LAG_SECONDS
        spans.append(CaptionSpan(name, max(0.0, start), event.timestamp_seconds))
    return spans


def caption_names(events: Iterable) -> list[str]:
    """Everyone the captions named, in the order they first spoke."""
    names: list[str] = []
    for span in caption_spans(events):
        if span.name not in names:
            names.append(span.name)
    return names


def _line_spans(lines: Sequence[TranscriptLine]) -> list[tuple[TranscriptLine, float, float]]:
    ordered = sorted(lines, key=lambda line: line.timestamp_seconds)
    spans = []
    for index, line in enumerate(ordered):
        end = line.timestamp_seconds + _MAX_LINE_SECONDS
        if index + 1 < len(ordered):
            end = min(end, max(line.timestamp_seconds, ordered[index + 1].timestamp_seconds))
        spans.append((line, line.timestamp_seconds, end))
    return spans


def _overlaps(start: float, end: float, captions: Sequence[CaptionSpan]) -> dict[str, float]:
    found: dict[str, float] = defaultdict(float)
    for caption in captions:
        overlap = min(end, caption.end) - max(start, caption.start)
        if overlap > 0:
            found[caption.name] += overlap
    return found


def _own_names(mic_lines: Sequence[TranscriptLine], system_lines: Sequence[TranscriptLine], captions) -> set[str]:
    """Names the captions show mostly while the microphone track is talking — the person recording."""
    mic: dict[str, float] = defaultdict(float)
    system: dict[str, float] = defaultdict(float)
    for totals, lines in ((mic, mic_lines), (system, system_lines)):
        for _line, start, end in _line_spans(lines):
            for name, overlap in _overlaps(start, end, captions).items():
                totals[name] += overlap
    return {name for name, seconds in mic.items() if seconds >= _LABEL_MIN_SECONDS and seconds > system[name]}


def name_speakers(
    mic_lines: Sequence[TranscriptLine], system_lines: Sequence[TranscriptLine], events: Iterable
) -> tuple[list[TranscriptLine], list[str]]:
    """`system_lines` with speakers named from the captions in `events` where they clearly can be (see the
    module docstring), and the names that were given out, in order of first use."""
    captions = caption_spans(events)
    if not captions or not system_lines:
        return list(system_lines), []
    own = _own_names(mic_lines, system_lines, captions)
    captions = [caption for caption in captions if caption.name not in own]

    spans = _line_spans(system_lines)
    by_label: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for line, start, end in spans:
        if line.speaker is not None:
            for name, overlap in _overlaps(start, end, captions).items():
                by_label[line.speaker][name] += overlap

    label_names: dict[str, str] = {}
    candidates = sorted(
        ((seconds, label, name) for label, names in by_label.items() for name, seconds in names.items()),
        reverse=True,
    )
    for seconds, label, name in candidates:
        if label in label_names or name in label_names.values():
            continue
        if seconds >= _LABEL_MIN_SECONDS and seconds >= _LABEL_SHARE * sum(by_label[label].values()):
            label_names[label] = name

    result: list[TranscriptLine] = []
    used: list[str] = []
    for line, start, end in spans:
        name = label_names.get(line.speaker) if line.speaker is not None else _line_name(start, end, captions)
        if name is None:
            result.append(line)
            continue
        result.append(replace(line, speaker=name))
        if name not in used:
            used.append(name)
    return result, used


def _line_name(start: float, end: float, captions: Sequence[CaptionSpan]) -> str | None:
    overlaps = _overlaps(start, end, captions)
    if not overlaps:
        return None
    name, seconds = max(overlaps.items(), key=lambda item: item[1])
    if seconds >= _LINE_MIN_SECONDS and seconds >= _LINE_SHARE * sum(overlaps.values()):
        return name
    return None
