"""Free-space checks for recording, so a full disk is caught before a meeting rather than during one.

Running out of space mid-meeting stops the WAV writers, and the rest of the meeting is lost however that's
handled afterwards — losing a call at minute 40 is strictly worse than refusing it at minute 0, when the
user can still free some space and start again. So a meeting won't start below START_FLOOR_BYTES, and a
recording that runs the disk down past WARN_FLOOR_BYTES says so while there's still time to act.

The recorder writes each track at the device's own format — typically 48 kHz stereo 16-bit, about 0.7 GB an
hour per track, so roughly 1.4 GB an hour for a meeting. START_FLOOR_BYTES is therefore about an hour and a
half of recording, and WARN_FLOOR_BYTES leaves about 40 minutes after the warning appears. (Finished
meetings' recordings shrink to a small fraction of that once transcribed — see audio_archive.)

A reading that can't be taken (a network drive, an odd path) never blocks anything: refusing on a guess
would stop the app doing its main job, and a genuinely full disk still shows up as a capture failure.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

START_FLOOR_BYTES = 2 * 1024**3
WARN_FLOOR_BYTES = 1 * 1024**3
# How often a recording in progress re-measures. The GUI asks several times a second; the answer can't
# change meaningfully faster than this, and a network-backed folder can be slow to ask.
_POLL_SECONDS = 10.0


class NotEnoughDiskSpace(OSError):
    """Raised instead of starting a meeting that would soon run out of room."""


def free_bytes(path: Path) -> int | None:
    """Free space on the drive holding `path`, or None if it can't be measured."""
    try:
        return shutil.disk_usage(path).free
    except (OSError, ValueError):
        return None


def _megabytes(count: int) -> str:
    return f"{count / 1024**2:,.0f} MB"


def check_room_to_record(path: Path, *, floor_bytes: int = START_FLOOR_BYTES) -> None:
    """Raises NotEnoughDiskSpace, with a message ready to show, if the drive holding `path` has less than
    `floor_bytes` free. Silent if there's room, or if free space can't be measured."""
    free = free_bytes(path)
    if free is not None and free < floor_bytes:
        raise NotEnoughDiskSpace(
            f"only {_megabytes(free)} free on the drive holding {path}, and a meeting needs about "
            f"{_megabytes(floor_bytes)} to be safe (roughly 1.4 GB an hour). Free some space and start again."
        )


class LowDiskWatch:
    """Watches free space while a meeting records. problem() is the warning to show while space is low
    (None otherwise); summary() says afterwards whether it ever was, for the finished meeting's log."""

    def __init__(
        self,
        path: Path,
        *,
        floor_bytes: int = WARN_FLOOR_BYTES,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = _POLL_SECONDS,
    ):
        self._path = path
        self._floor_bytes = floor_bytes
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._checked_at: float | None = None
        self._problem: str | None = None
        self._lowest_free: int | None = None

    def problem(self) -> str | None:
        now = self._clock()
        if self._checked_at is not None and now - self._checked_at < self._poll_seconds:
            return self._problem
        self._checked_at = now
        free = free_bytes(self._path)
        if free is None:
            return self._problem  # a failed reading must never look like space coming back
        if free >= self._floor_bytes:
            self._problem = None
        else:
            self._problem = (
                f"Disk nearly full: {_megabytes(free)} left — about {free / (1.4 * 1024**3) * 60:.0f} minutes "
                "of recording. Free some space now, or the rest of the meeting won't be recorded."
            )
            self._lowest_free = free if self._lowest_free is None else min(self._lowest_free, free)
        return self._problem

    def summary(self) -> str | None:
        """A line for the meeting's log if the disk ran low while it recorded, None if it never did."""
        if self._lowest_free is None:
            return None
        return (
            f"The disk ran low while recording ({_megabytes(self._lowest_free)} free at its lowest). If the "
            "recording stops short, that's why."
        )
