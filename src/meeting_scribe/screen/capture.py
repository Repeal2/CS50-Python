"""Periodic screenshot + OCR, so on-screen captions/chat/slides make it into the transcript even when
nobody says them out loud.

Runs on a background thread, screenshotting at `interval_seconds` and only paying the OCR cost when the
frame actually changed (a cheap content hash short-circuits static screens). Dedup happens at the *line*
level, not the whole-frame level: a scrolling chat/captions pane re-shows most of its previous content on
every capture (nothing scrolled off, or only the top line did), so comparing whole OCR blobs means almost
every capture looks "new" and the transcript balloons with the same messages repeated over and over. A
persistent set of already-emitted lines means only genuinely new lines ever become a `ScreenTextEvent`,
delivered to the `on_text` callback, timestamped relative to when watching started so it can be
interleaved with the audio transcript by `transcription.engine`.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable

from meeting_scribe.screen.region_picker import RegionTarget
from meeting_scribe.screen.window_picker import WindowTarget

# Tuned to reject short/symbol-heavy OCR misreads of UI chrome (avatar initials like "MB", icon rows
# like "@ B x") without also rejecting genuinely short spoken/chat lines ("On T3."). Not reliable against
# a longer garbled misread that happens to look like real words (e.g. a decorative divider line OCR'd as
# "np sewn ee ee ener meee eee") — that would need something like a dictionary check, which this doesn't
# attempt.
_MIN_ALPHA_CHARS = 3
_MIN_ALPHA_RATIO = 0.5


def _looks_like_ui_noise(line: str) -> bool:
    letters = sum(1 for ch in line if ch.isalpha())
    non_space = sum(1 for ch in line if not ch.isspace())
    if non_space == 0:
        return True
    return letters < _MIN_ALPHA_CHARS or (letters / non_space) < _MIN_ALPHA_RATIO


def _new_lines(text: str, already_seen: set[str]) -> list[str]:
    """Splits one capture's OCR text into lines and returns only the ones that are new: not blank, not
    UI noise, and not in `already_seen` (or already returned earlier in this same call, in case a line
    repeats within one capture). Order matches first appearance in `text`. Doesn't mutate `already_seen`
    — the caller adds the returned lines once it's decided to use them."""
    new: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _looks_like_ui_noise(line) or line in already_seen or line in new:
            continue
        new.append(line)
    return new


@dataclass(frozen=True)
class ScreenTextEvent:
    timestamp_seconds: float
    text: str


class ScreenWatcher:
    def __init__(
        self,
        on_text: Callable[[ScreenTextEvent], None],
        interval_seconds: float = 3.0,
        tesseract_cmd: str | None = None,
        target: WindowTarget | RegionTarget | None = None,
    ):
        self._on_text = on_text
        self._interval = interval_seconds
        self._tesseract_cmd = tesseract_cmd
        self._target = target
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_frame_hash: str | None = None
        self._seen_lines: set[str] = set()

    def start(self) -> None:
        self._stop_event.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="screen-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        import mss
        import pytesseract
        from PIL import Image

        if self._tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd

        with mss.mss() as sct:
            while not self._stop_event.is_set():
                loop_start = time.monotonic()
                region = self._resolve_region(sct)
                if region is not None:
                    self._capture_once(sct, region, Image, pytesseract)
                elapsed = time.monotonic() - loop_start
                self._stop_event.wait(max(0.0, self._interval - elapsed))

    def _resolve_region(self, sct) -> dict | None:
        """Returns the mss region to capture: the whole virtual screen, a fixed user-drawn rectangle,
        or the selected window's current bounds — None if a selected window has since closed or been
        minimized, in which case the caller skips that capture cycle rather than falling back to the
        whole screen."""
        if self._target is None:
            return sct.monitors[0]  # index 0 == a single virtual monitor spanning all displays
        if isinstance(self._target, RegionTarget):
            return self._target.mss_region
        from meeting_scribe.screen.window_picker import get_window_region

        return get_window_region(self._target.hwnd)

    def _capture_once(self, sct, region, Image, pytesseract) -> None:
        shot = sct.grab(region)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        frame_hash = hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()
        if frame_hash == self._last_frame_hash:
            return  # screen hasn't changed since the last capture; skip the OCR cost
        self._last_frame_hash = frame_hash

        text = pytesseract.image_to_string(image)
        new_lines = _new_lines(text, self._seen_lines)
        if not new_lines:
            return
        self._seen_lines.update(new_lines)
        elapsed = time.monotonic() - self._started_at
        self._on_text(ScreenTextEvent(timestamp_seconds=elapsed, text="\n".join(new_lines)))


def ocr_region(region: dict, tesseract_cmd: str | None = None) -> str:
    """One-shot OCR of a fixed screen rectangle (an mss-style region dict — see RegionTarget.mss_region).
    Unlike ScreenWatcher this isn't a continuous background watcher; it grabs exactly one frame right now
    and returns whatever Tesseract reads from it, for on-demand captures like reading a meeting's
    attendee list off a participants panel."""
    import mss
    import pytesseract
    from PIL import Image

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    with mss.mss() as sct:
        return _ocr_region_once(sct, region, Image, pytesseract)


def _ocr_region_once(sct, region, Image, pytesseract) -> str:
    """The actual grab-and-read, split out from ocr_region() so it's testable with fakes the same way
    ScreenWatcher._capture_once is. Runs the same noise filter as the live OCR stream (an empty
    `already_seen` set degrades _new_lines to "just drop blank/junk/duplicate lines"), since
    avatar-initial and icon-row garbage shows up in a one-shot capture exactly like it does in a
    continuous one."""
    shot = sct.grab(region)
    image = Image.frombytes("RGB", shot.size, shot.rgb)
    text = pytesseract.image_to_string(image)
    return "\n".join(_new_lines(text, already_seen=set()))
