"""Periodic screenshot + OCR, so on-screen captions/chat/slides make it into the transcript even when
nobody says them out loud.

Runs on a background thread, screenshotting at `interval_seconds` and only paying the OCR cost when the
frame actually changed (a cheap content hash short-circuits static screens). Each *new* piece of text
becomes a `ScreenTextEvent` delivered to the `on_text` callback, timestamped relative to when watching
started so it can be interleaved with the audio transcript by `transcription.engine`.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable


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
    ):
        self._on_text = on_text
        self._interval = interval_seconds
        self._tesseract_cmd = tesseract_cmd
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_frame_hash: str | None = None
        self._last_text = ""

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
            monitor = sct.monitors[0]  # index 0 == a single virtual monitor spanning all displays
            while not self._stop_event.is_set():
                loop_start = time.monotonic()
                self._capture_once(sct, monitor, Image, pytesseract)
                elapsed = time.monotonic() - loop_start
                self._stop_event.wait(max(0.0, self._interval - elapsed))

    def _capture_once(self, sct, monitor, Image, pytesseract) -> None:
        shot = sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        frame_hash = hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()
        if frame_hash == self._last_frame_hash:
            return  # screen hasn't changed since the last capture; skip the OCR cost
        self._last_frame_hash = frame_hash

        text = pytesseract.image_to_string(image).strip()
        if not text or text == self._last_text:
            return
        self._last_text = text
        elapsed = time.monotonic() - self._started_at
        self._on_text(ScreenTextEvent(timestamp_seconds=elapsed, text=text))
